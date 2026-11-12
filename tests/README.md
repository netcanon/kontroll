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
| **L2 fresh-node check** | a brand-new node's install evaluated by Ansible in check mode — `deploy-stack.yml` (with the `local-canonical.yml` it imports; with every service it must fail for the missing onboard-gui key, then pass with a throwaway one) and `bootstrap.yml`, with `KONTROLL_IN_CONTAINER=1` and an operator named, as the installer runs them, against the instance a real `kontroll-init --fresh` writes, in a throwaway export of HEAD made a repository on `main`, as a fresh clone is — then `ping.yml` and `backup-configs.yml` against `mock-inventory.yml`, one host per shipped device class: ping must rescue, and backup fail, exactly the hosts the script lists, each at its task (a read that reaches the device; backup's summary names it, I-12), ping's each for its reason; the API class's host, given a placeholder token, must run past its credential gate to OK and to its capture, and a pass of each for that host alone without it must stop it there; and backup must write no capture. No device, no container | `tests/fresh-node-check.sh` | CI (`validate` job); not in `validate.sh`, which also runs on a configured control node |
| **L2 vector test** | the internal Vector shapes' VRL run on crafted events — each field the Loki sink templates as a label is set, and the `level` a shape derives (`docker/vector/tests/*.yaml`, one event per test) | `validate.sh` step `vector-test` (`vector test`) | CI (`validate --strict`), the gate container |
| **L2 pytest** | `galaxy.py` + GUI logic, shell-outs mocked; a few rows run git or ansible-playbook (no lab) | `pytest` (`unit/` + `integration/`) | on change, CI |
| **L2 refusal run** | the connection refusal (`ansible/playbooks/tasks/refuse-local-connection.yml`) run by the installed ansible-core over the connection spellings its reads measured: only a host on the importing play's allowlist gets past it, and every later task runs over the connection it passed; on a core from 2.21 on, which removed paramiko_ssh, the two hosts on it fail at the plugin load (`integration/test_refuse_local_connection_runtime.py`); `wire-logging.yml` run for each logging method: only a host on that method's `wiring_connections` reaches its role, with `ansible_connection` still the value the refusal passed and on no delegate, and the key cleanup then runs, with a method list seeded beside the playbooks that it does not read (`integration/test_wire_logging_allowlist_runtime.py`); the device_role fact guard (`ansible/playbooks/tasks/refuse-device-role-fact.yml`) run after an `update_fact`, a `library/` module and a cacheable `set_fact`: the host each set the role of is refused, a host whose role only the inventory names passes (`integration/test_device_role_fact_guard_runtime.py`); the entrypoint check (`ansible/playbooks/tasks/refuse-missing-entrypoint.yml`) beside a real include_role, for each place and shape the role search takes (`integration/test_refuse_missing_entrypoint_runs.py`), and ping.yml's refusal of a class with no check entrypoint (`integration/test_ping_refused_class_runs.py`), and its rescue telling a failed probe (UNREACHABLE) from a failed check (CHECK FAILED, which fails the run) (`integration/test_ping_check_failed_runs.py`); all skipped without ansible-playbook | `validate.sh` step `refuse-local-runtime` (a skipped row fails it) | CI (`validate --strict`), the gate container |
| **L2 as root** | the canonical's git as the service uid, with real uids on a canonical laid out as the play leaves one (`integration/test_canon_drop.py`), the deploy's content-clone tasks run by ansible-playbook as root (`integration/test_content_clone_as_service.py`), deploy-stack's age-key gate on keys of uid or gid 1001 (`integration/test_g9_age_key_gate_real_stat.py`), and the stale-config rows that give a store file another owner (`unit/test_stale_config.py`, by node id); skipped unless root on Linux. A row there that starts a child as ANOTHER uid starts `sh` or a system program, or picks an account that can run the suite's interpreter (`_a_third_account`): a runner's Python may live where only its owner goes | `validate.sh` step `canon-drop-as-root` (directly as root, or through `sudo -n`; a skipped row fails it) | CI (`validate --strict`), the gate container |
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
# L2 fresh-node check: the install and the fleet plays evaluated in check mode, in a throwaway export of HEAD
# (needs ansible-playbook with every module's collections — `python3 scripts/gen-requirements.py <file>
# --all-modules`, then `ansible-galaxy collection install -r <file>` — sops, age-keygen, git, and for the deploy
# passes the `docker` Python library, buildx and a Docker daemon to read; KONTROLL_FRESH_KEEP=1 keeps it)
bash tests/fresh-node-check.sh
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

## A row that reads the tree through git (`requires_checkout`)

A few rows read the repository through git, not through the files on disk: the index's modes, the tracked list,
what is ignored, a `git archive HEAD` build. Each carries `@pytest.mark.requires_checkout` (a module-level
`pytestmark` works too), and the conftest hook skips them, with the reason, where the tree is not a git checkout: a
`git archive` copy (every container check runs on one) or a source tarball. [_checkout.py](_checkout.py) gives the
answer. The tree is a checkout when git names it ITSELF as the top of its work tree; the reason is git's own "not a
git repository", a copy inside another repository's directory, a `GIT_DIR` handed down that names another repository,
no git on the PATH, or git not answering. A tree git finds and REFUSES (dubious ownership: another uid owns it) has no
reason: its rows run and fail with git's own error. Before the marker, those rows FAILED outside a checkout with a
CalledProcessError that said nothing of why (I-51).

Where `KONTROLL_REQUIRE_CHECKOUT=1` a reason is a session error instead of a skip: validate's pytest step and CI's
offline pytest job set it, since both run in a checkout, where a skipped marked row would pass a gate that never ran it.

A new row that shells out to git about this tree takes the marker, unless it can read the files on disk instead
when git cannot list them and stay a whole check on an archive (as the doc-link and bind-source checks do); a row
that builds its own repository in `tmp_path` does not need it. `unit/test_requires_checkout.py` pins the answer, and
runs the marked files in tests/unit and tests/integration (the two it scans) in a copy that is not a checkout.

## Adding tests

1. **L1**: nothing to add — new YAML is linted automatically. Keep it valid.
2. **L2 syntax**: a new playbook is picked up by `validate` automatically.
3. **L2 pytest**: drop a `test_*.py` under `unit/` (pure logic) or `integration/`
   (a command driven as a black box). Mark it `@pytest.mark.unit`/`integration`, and a row that reads this tree
   through git `requires_checkout` (see [the section above](#a-row-that-reads-the-tree-through-git-requires_checkout)).
4. **L2 vector test**: a new internal Vector shape (`docker/vector/config.d/transforms/`) gets a case in
   `docker/vector/tests/` — one event in, the label fields asserted out. The static row in
   `unit/test_compose_logging.py` already holds it to setting every templated label.
5. **L2 check mode**: a new device class gets a host in `mock-inventory.yml`, in its `inventory_group`, with its role
   as `device_role` — `unit/test_mock_inventory.py` fails until it has one of its own, even where another class shares
   its group and role — and the fresh-node check then evaluates its `check` and `backup` entrypoints in CI. If its
   check or its backup reads the device under `ansible_connection: local`, list the host and the task it stops at in
   `fresh-node-check.sh`'s ping step (the hosts ping rescues, each with a `needs` line for the reason it is rescued)
   or backup step; the check fails until it is listed. A class that needs a credential a fresh node has none of gets
   a placeholder in those passes, as mock-fortigate's token, and a pass without it that holds its gate. Dry-run a play
   by hand with the `--check --diff` command above.
6. **L3 smoke**: add a read-only assertion play (service responds / target `up`)
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

### A canned shape lives once, in `tests/_canned.py`

A dict a test is served in place of a real service call — a seam the e2e server replaces, a route test's
`monkeypatch` return, a `page.route` body — belongs once in [_canned.py](_canned.py), and
[unit/test_mock_conformance.py](unit/test_mock_conformance.py) holds a row for it that builds the smallest real
fixture, calls the real function and compares the two SHAPES: the key set and each value's type, never the values.
Only the seams are enforced (below). A `page.route` body or a route test's fake is held by its row once it is in
`_canned.py`, but nothing scans the tests for one written by hand, so a new one is caught only in review. Still
written by hand: `e2e/test_pending_changes.py`'s `_payload` / `_proposal` builders (conformant to `list_pending`
when T21's read measured them), the onboard flow's error stub, and route-test fakes —
`integration/test_gui_api.py`'s `promote_board`, `discard_proposal`, `module_for_collection` (`_MODULE_LOW` /
`_MODULE_HIGH`) and `local_installed`; `integration/test_api.py`'s `actuation_unit` (it lacks `schema` and
`install`, so the registry's validator would refuse it), `units_in_collection` and `local_installed`;
`integration/test_api_create_unit.py`'s `module_for_collection`; `integration/test_api_capability.py`'s
`_fake_probe` (a deep_probe answer written as a literal, not with `probe._facts`); and the `local_installed` returns
in the other `integration/test_api_*.py` files and `test_onboard_dryrun.py`. The answers some e2e tests hand a
renderer through `page.evaluate` are written by hand too. Move one into `_canned.py`, with a row, when you next touch
it. A fake whose answer has no shape to drift (an empty dict or list, `None`, a bool, a list or set of names:
`_enabled_modules`, `_existing_keys`, `sops_decrypt_domain`, `sops_write_domain`, `service_units`) and a stand-in
for an external tool's output (`catalog._installed_json`) are not listed.
A key whose value is by design one type or another is produced in both types by the row's fixture (the inbox's
`vendor`), or, where the row's fixture does not, named in the row's `optional` set (the Pending rows'
`repropose_hint`, whose canned value has a row of its own). A mock written from belief keeps a UI test green while
the server emits something else, and a second hand copy drifts from the first (S33). A seam the e2e conftest
replaces with no row fails `test_every_replaced_seam_has_a_conformance_row`, and so does one it replaces without
handing it back to be restored, or one replaced in a form the parse refuses (a nested target, a `setattr` by a
variable name or handed on, a monkeypatch object's `.setattr`, an assignment through an imported name outside
`_mock_service_seams`, a `unittest.mock` import, a write through `__dict__`, `__setattr__`, `vars`, `exec` or
`eval`), and so does a conftest without its one restore loop or without a `return originals` in
`_mock_service_seams`. Anything else is not read, so review holds it: for example a replacement outside
`_mock_service_seams` through a name that is not an import, a fixture that patches (`mocker`), one made in another
file, and whether the restore loop puts back what it was handed. The `_seams` docstring names more. A seam
whose fake needs none (a record built by the real constructor, an empty list) is listed in `NO_ROW` with the
reason. A seam hands out a copy of its canned dict, never the module object. Where a generator writes a file
another part reads, a round-trip test feeds the real output to the real reader:
`test_configure_semaphore.py::test_reads_what_gen_backup_renders`,
`test_provenance.py::test_reads_the_sidecar_gen_requirements_writes` and
`test_pending.py::test_lists_a_proposal_the_real_stager_produced`. The C19 paint suite (`e2e/test_paint_rule.py`)
is outside the rule: it serves payloads made to carry markup in every field, not a producer's answer.

```
tests/unit/          # pure functions, no I/O
  test_predicate_engine.py   # three-valued eval_pred/eval_vector truth tables
  test_classify.py           # backend dispatch from synthetic facts
  test_search_card.py        # the Generic SSH host card: matched, never probed or pinned, malformed ones skipped
  test_build_record.py       # record schema + overrides (incl. YAML yes/no→state)
  test_telemetry_vector.py   # the telemetry capability vector (4th vector): 3-state + depth-stable
  test_telemetry_suggest.py  # suggest_telemetry + declared_metrics_methods (detected/declared bridge)
  test_fleet_edit.py         # enable_in_fleet idempotent, comment-preserving
  test_openapi.py            # derive_auth/derive_server/classify_endpoints heuristics
  test_service_layer.py      # service_* return structured data directly (no stdout-parsing)
  test_mock_conformance.py   # S33: every canned shape in tests/_canned.py (bar `E2E_UNIT`, held by the registry's validator) has the key set + value types of the real function's answer; every e2e-replaced seam the parse reads has a row or a `NO_ROW` reason
  test_units.py              # runnable-unit prober + service_units resolver + SEC-3 traversal guard + validate_unit_config (R3 configure gate)
  test_actuation_registry.py # actuation/ registry: resolve_pins (exact-wins/fail-closed) + gen-actuation validation (incl. R3 curated knobs) + overlay loader
  test_actuation_service.py  # R4 preview + R5 write verb: build_actuation_plan render/staging-metadata (read-only) + apply_actuation_plan (idempotent write) + stage_plan drift gate
  test_discovery.py          # discovery inbox (Rung 1a/2/3): registry load + declared offers reach opnsense/fortigate/routeros/cisco_ios + json_rows (results-key & bare-array, Bearer/Basic) + snmp arp_table (transport dispatch, snmp_walk byte-cap, MAC/suffix parse) + read-only 2-primitive fetcher surface + sweep parse/cap + read_inbox IP-diff/normalize + read-only-pin canary
  test_request_boundary.py   # C22: the request-field validators' accept/reject tables, the joins (the probe's argv, every plan builder), refusals that never quote the value
  test_api_audit.py          # one request value is one cell of one audit row: the shared flattener, and the API writer read back through a real file
  test_repository_data.py    # C22: a name adopted from a module file, the reserved names at each seam, the scaffold's arguments, and the refusal that is the repository's own (409), never a field's
  test_yamlscalar.py         # may a string stand unquoted in a hand-formatted YAML line: every position a writer uses, exhaustively for short strings; the quoting fallback of the generators
  test_staging_isolation.py  # C10: the staging clone returns to canonical main after each stage; and the canonical's update hook — `main` refused from EVERY uid (it is landed or promoted), the committed file run over an argument table under every POSIX shell installed (one that runs its own `id` before the PATH's — Debian's busybox — is left out: the table says who pushes with an `id` of its own), its run-id rule against canon.valid_run_id, real pushes into a repository that carries it (the hook's execution tests need POSIX sh: skipped on Windows; the hook only ever runs on Linux, and CI does)
  test_promote_hint_single_source.py # T19: every staged write's `next` takes its promote instruction from pending.promote_hint; no surface or document spells a dead command or a bare run id
  test_promote_cas.py        # C10: the promote is one ref transaction over the two ids that were read — the races injected between the read and the move, against real git
  test_land.py               # C10: the landing — every door into the canonical's `main` is `gitio.land_commit` (judged by the promote gate, a pack on a pipe, one compare-and-swap), never a push: the library, the command, `land_main` (the CLI's seam — patched beside `_run` in a mocked test, else it lands on the checkout's own `local` remote), `commit_and_push` outside staging, canary hooks and programs in the canonical that must NOT run (with a push as the control), the pin on the canonical-side verbs; a shallow clone refused before the gate and the pack, with the remedy, where the canonical lacks what it cut off (a rewritten stop, a partial clone's missing blob, which fetches nothing, a linked worktree, a walk that cannot finish), and landing where the canonical holds it (on top of `main`, a merge's cut side, a proposal) (I-20); the two shell doors under bash (Linux: the gate image)
  test_canon_tree.py         # C10: one commit's tree read object by object and re-hashed — attributes, filters, a swapped object, a link, the caps; the export command (a process stood in for git starts in the test's own environment, not the hardened one: `_in_gits_place`)
  test_canon.py              # C10: how the canonical is read — refs by full name, one canary test (with its control) per entry of the git environment a planted file can show, the whole entry list held by name, bytes to text, the seam pin
  test_canonical_audit.py    # C10: the audit of a canonical — a canonical the play made, the tool used and git collected garbage in is clean; each planted thing named by kind and key and never by value; names escaped and cut; no link followed, no exchanged directory listed, no pipe waited on; the one process held by a closed list; every kind in both tables; the play's tasks and their conditions, evaluated
  test_canonical_normalise.py # C10 / F-CANON: the play's ownership pass (`--normalise`) — `--check` names and changes nothing; applied, the bits are set and a second run has nothing to do; a link to a file and to a directory outside the tree named and what they point at left as it was; a pipe named and not waited on; hooks/ passed over; a directory exchanged for a link after it was looked at not gone into; the refusal where no owner or mode can be changed through a descriptor; one walker for the audit and the pass
  test_content_clone_tasks.py # C10: the deploy's git in a content clone — one task file for both clones; root's git only unarmed, the service uid's (setsid/setpriv/env -i) armed; a clone held by the wrong uid removed, never chowned; refs by full name
  test_fleet_line.py         # C22: a fleet line that is not a class key at every deploy-time reader — skipped and named by what renders, refused by what installs and by the promote gate, checked first by the plays
  test_bind_sources.py       # every relative bind source and env_file of a stack service: in the tree, an `instance/` path the scaffold ships, or written by deploy-stack before its one bring-up whenever the service (or a service that starts it) runs — planted conditions (a secrets-present test, another service, a conjunction, the block's) and late writers flagged
  test_check_mode_resolvers.py # every read-only command the control node runs (its plays, delegated tasks, imported files, applied roles) registers and reads again runs under `--check`, or has a reasoned exemption that is not stale; planted resolvers flagged, a device play's not; no device role's read forced to run in a dry-run
  test_mock_inventory.py     # T27: a mock host per shipped class (derived from modules/), each dispatching to a role with both fleet entrypoints, and each class exactly one host of its own; every address RFC-5737, every host `connection: local` (no group or host setting another)
  test_fresh_node_check_script.py # T27: tests/fresh-node-check.sh with fake tools — every step in a throwaway export with HOME and TMPDIR there, an operator named and the host's journal directory given as the launchers give it (I-25), the checkout untouched; the every-service pass must fail for the age-key reason, then pass with a throwaway key in a storage root inside; a pass needs a recap with no failed or unreachable host, a finished kontroll-init and its play's landmark; ping must rescue, and the backup pass fail, exactly the listed hosts, each at its task (ping's for its reason; backup's in its summary line, I-12), mock-fortigate run past its credential gate with a placeholder token and stop at it without, ping's words before I-84 and I-85 (a bare `OK`, `UNREACHABLE` at the gate) failing the ping steps, and the backup pass write no capture; a missing collection's failure says how to install every module's set
  test_backup_capture_check_mode.py # T27: the raw-SSH, RouterOS, API and CLI capture writes render an empty capture in a --check run (their read is skipped: the CLI's by its own `when`, for a command that is not a `show`, I-11), and a real run's exact output, a result with no `stdout` or `content` still failing; no role's capture write forced to run in check mode
  test_fleet_play_rescue.py  # I-64: install-node-exporter.yml's and wire-logging.yml's dispatching play refuses, then runs its work in one rescued block (the record names the failed task alone, a host with no failed task of its own fails, the host's play ends; no `failed_when`, `ignore_errors`, `tags` or `loop` there; `ignore_unreachable` true); summaries after the key cleanup fail each recorded host and connect to nothing; the validation play's rescue records the refusal, which the device play reads first; I-88: wire-logging.yml's guard play after the cleanup fails a request no validation play read
  test_backup_controller_plays.py # I-90: every play of a fleet playbook whose `hosts` names localhost (a string or a list) starts from `localhost:all`, runs once under the linear strategy and delegates every task to localhost (backup-configs.yml's controller plays among them; wire-logging.yml's localhost plays, which its guard covers, excepted by name)
  test_backup_summary_is_last.py # I-9: backup-configs.yml's one play that fails a host for a failed capture is its last, after the snapshot, the prune and the key-cleanup import; every play after the cleanup (in ping.yml too, its last play: I-83) gathers no facts, runs no role and holds only controller-side actions (debug, assert, fail, set_fact, meta), in blocks too, with no delegate_to
  test_backup_capture_rescue.py # I-12: backup-configs.yml's capture play dispatches inside a block whose rescue records a capture that fails as a task as FAILED (kontroll_backup_failed), naming the task and nothing of its result, and fails a host whose include could not be loaded; the play sets any_errors_fatal: false and no other play keyword; the device_role refusal stays outside it; I-66: the block's first task is the entrypoint check (tasks/refuse-missing-entrypoint.yml, its kontroll_entrypoint the include's tasks_from), one quiet assert whose first conditions are that device_role is defined, a string and not empty
  test_ping_check_failed.py # I-84/I-85: ping.yml's probe registers `_kontroll_probe`, and the probe block's rescue records `kontroll_check_failed` (a failed task, the probe not failed) and the failed task's name only (no `ansible_failed_result` anywhere in ping.yml); the summary's line names REFUSED, CHECK FAILED with that task, OK or UNREACHABLE in that order, `(dry run: the port was not probed)` under check mode; the last play's refusal of a dry run given `kontroll_ping_require_reachable`, then its three conditions (not refused; a dry run or no failed check; reachable where that var is true) and its defaulted message; galaxy.py's VERIFY_PING, VERIFY_ENV (a `--limit` that matches no host fails the run), the onboard's commit message naming the require var, and verify_line (OK only for rc 0); `gitio._run`'s `env` adds to the caller's environment for one command
  test_ping_entrypoint_refusal.py # I-83: ping.yml's reach play runs the device_role fact refusal, then a block holding only the entrypoint check (its kontroll_entrypoint the include's tasks_from, `check`) whose rescue only records kontroll_class_refused, then the probe's block, skipped for a recorded host; the summary names it REFUSED before reading kontroll_reachable; the run's last play, after the key-cleanup import, is I-84's dry-run refusal and one assert that fails it; the probe's rescue fails a host whose check could not be loaded (no failed task) and only that; the reach play sets `any_errors_fatal: false`
  test_backend_api_check_mode.py # I-6: the API check's refusal and the backup's failure record take a read check mode skipped as not made (not one a `when` skipped), and every real answer but a success as before; the capture's record follows its write; both dry-run reports name only the host and the recipe; every task reading a registered result is one it evaluates; no read forced to run in a dry-run
  test_capture_no_log.py      # I-13: every capture write, and each read in its file whose register it names, hides its output (`no_log: true` on the task, or in an include's `apply:`), or is exempt for the file it reads (no credential); every task is checked, a second of one name too (found as the I-5 test finds them, .yml or .yaml); each exemption names tasks that exist and read its file
  test_backup_empty_capture_refused.py # I-5, I-10: each backup refuses a read that answered nothing (empty, or only blank lines; for Proxmox and Docker, the `### ` headers their script echoes alone) before its write — recorded FAILED, the run failed, the last good capture kept, "captured" recorded only where the write ran — and a dry-run refuses nothing it did not read, nor an unreachable host's; each refusal judges the role's one read; every role task naming config_backup_dir (at any depth under tasks/, or through a fact) is a refusing write or exempt for a reason that is checked; I-62: a Docker read that exits non-zero, or fails with no exit code, records it alone as FAILED, before the write, setting only the result and the flag; I-11: the CLI backend does not read a command that does not start with `show` in a dry-run (cli_command's own rule: case, a leading space and `sh run` count), notes it and previews the write, reads the default and every `show` there, and reads every command in a real run
  test_backend_api_backup_failure_message.py # I-17: the API backup's failure record names its cause for each auth type and answer (the check's auth hint word for word, no answer, an answer that is not a 200, each recorded as a failure), and reads only the status, the backup path and the names its auth reads (no loop, no other var)
  test_launcher_operator_home_binds.py # I-23: every bind a service takes from the operator's home (read from docker/services/*.yaml, the short form in each spelling and the long form; none naming the home another way) exists, the user's, when each launcher first calls docker (a stand-in), in an empty home, for each verb; the run-log chain o+rx; a launcher whose run-log dir cannot be made warns and goes on; the deploy's task comes before the play's `up`
  test_run_log_dir.py        # I-29: scripts/lib/run-log.sh writes where Vector's run-log bind reads (the path read from docker/services/vector.yaml), whatever XDG_STATE_HOME says (unset, outside HOME, under HOME, where an earlier helper left logs; the helper's code names it nowhere); a log kept under KONTROLL_RUN_LOG_DIR says in its first lines, on the console and in the log, that Vector does not read it, and the shipped directory however spelled says nothing; HOME unset, an override that cannot be made, the prune's directory (bash: Linux, the gate image)
  test_compose_ensure.py     # I-21: install-prereqs asks `docker compose` before installing a compose package (pkg.sh's compose_ensure, under sh with stand-in docker/runuser/apt-cache/apt-get/dnf/zypper/pacman): nothing asked of apt when it answers, as root or as the operator; the first of Docker's plugin, Ubuntu's docker-compose-v2 and Debian's docker-compose (2.x only) the sources offer, read in the C locale and never removing a package; a refusal naming them, and on other managers saying it installs compose only on apt; and install-prereqs.sh run whole with stand-ins for its root-only commands: without compose it installs the rest of the floor and exits 1 before Done; its text keeps the call after every Docker path
  test_installer_stdin.py   # I-4: what stdin each launcher hands compose `run`, both launchers run with a stand-in docker that records its arguments and its stdin: with a line on a pipe or in a regular file, every verb but `shell` gets /dev/null and the command after the launcher in the same shell gets the line; `shell` keeps it; a pty reaches `init` and `shell` as a terminal; each row checks the verb compose got; each launcher's two run lines (the only lines that run compose `run`) are one command but for the redirect
  test_operator_uid_1001_refused.py # I-18: with `id -u` 1001 (a stand-in id), each launcher refuses every verb that deploys or scaffolds before docker, any mkdir or its canonical-group `sudo` (stand-ins write each call down), `shell` told and let on, any other uid let on, its operator the account running it whoever ran sudo; install-prereqs.sh refuses under sudo (SUDO_UID, read as root only) and without, before its root check, and bootstrap.sh as root hands it the outer sudo's operator, beside a stand-in lib/ that installs nothing; troubleshooting §10 quotes the three sentences. The launcher test files take `not_the_service_uid` (tests/conftest.py), a stand-in id, so a runner whose own uid is 1001 decides nothing
  test_storage_root_floor.py # I-24: install-prereqs.sh's helper makes a missing storage root and each missing parent root's 0711 (exactly: an inherited group or setgid bit is given back; AS ROOT), leaves one that is there as it is and says what it is (a 0700 and a 0755 dir, a file, a link to nothing; a root:root one others can list gets `sudo chmod 0711`, AS ROOT), refuses `/`, a relative path, a root under a directory others can write that is not sticky (AS ROOT; under a sticky one it is made, AS ROOT) and one under another account's directory, the parent, further up, where a link leads or holding the link, fails on a parent that is a file (AS ROOT); install-prereqs.sh run whole AS ROOT with stand-ins (pacman, docker, getent) on a PATH of nothing else: the root made before any package and 'Done'; KONTROLL_STORAGE_ROOT read, --storage-root winning; a failing package step leaves it made; a refused or unmade root said with its remedy and as NOT made, the run still done; without root nothing made; --storage-root with no path, and an unknown argument, refused; its default is the installer's and the deploy's; the AS ROOT rows run in validate.sh's as-root step, which deselects the one row that needs a non-root user
  test_bootstrap_summary_lockfile.py # T27: bootstrap's summary on a fresh tree with no collection lockfile reads none in a --check run and says why; with one, lists it; a real run with none still reads it and fails
  test_run_id_stamp_imported.py # I-15, I-41: which playbooks print the run id, read as YAML by the path each import runs — every top-level playbook (`.yml` or `.yaml`) whose name does not start with `_` imports the stamp or the fleet preamble first (no list kept; a subdirectory one is refused by test_group_dispatch_refuses_local.py); each importer of the stamp (the preamble, deploy-stack, promote, reload-observability, bootstrap, local-canonical among them) imports it first, once, with nothing but its name on it; every playbook that imports the preamble does so first and once, and does not import the stamp as well; deploy-stack imports local-canonical after its own stamp; the stamp prints once (its guard and fact), on the controller (`delegate_to: localhost`), with `tags: [always]`, and sets no id
  test_installer_journal_dir.py # I-25: each launcher (no verb, `check`, `shell`; a stand-in docker; its probe pointed at a stand-in for /var/log/journal in each shape) exports KONTROLL_HOST_JOURNAL_DIR as the host's own answer — the persistent journal where the path is a directory or a link to one, else the volatile one — over a value exported in the shell; the three scripts hold the same probe; installer.yaml passes it into the installer
  test_journald_role_flush.py # I-25: the logging_journald_remote role notifies a restart of journald and then a `journalctl --flush`, so /var/log/journal is there for the next deploy
  test_requires_checkout.py # I-51: tests/_checkout.py's answer (a plain tree, a checkout's top and one reached through a link, a copy inside another repository, a `GIT_DIR` handed down, a checkout git refuses, no git) and the `requires_checkout` marker — registered, every marked row and no unmarked one skipped by the hook with the reason exactly when this tree is not a checkout; in a copy that is not one, run from another checkout, the marked files skip, none failing, an unmarked row runs, and KONTROLL_REQUIRE_CHECKOUT=1 is an error; validate and CI set it
  test_no_log_censor.py # I-75: the runner Dockerfile COPYs docker/semaphore-runner/no_log_censor.py and runs it as root with the base's ansible venv Python, in a RUN whose failure fails the build, with no later step that runs pip or names ansible-core; the script drops 'exception' alone from the censor's `_PRESERVE` line (the other keys in order, a tuple kept a tuple, a notice line above it, nothing written on a core without it) and refuses every other shape, 'exception' alone among them, naming where each core keeps the censor; its check, against a stand-in core, reports the marker on stdout, on stderr or in the log only, a run that passes or prints no FAILED!, no hidden-output notice, and a control that prints nothing
tests/integration/   # galaxy.py / gui as black boxes, shell-outs mocked; the AS ROOT rows and the backup, reload, run-log-dir and journal-dir runs call the real tools
  test_local_canonical_as_root.py # C10, AS ROOT: local-canonical.yml run by ansible-playbook on a scratch node (the installer's mode: root, KONTROLL_IN_CONTAINER=1) — a fresh node's dry-run does not fail (nor one whose canonical path is the empty directory a bind mount leaves, or holds a mounted filesystem's `lost+found` and no repository, whose entries the ownership pass names, or a repository whose HEAD is gone, which its note says the real run restores, or a link to an empty directory or to nothing, which the first apply lands in the directory it names; a link to a landed canonical is audited, judged and deploys 0 changed — I-40); a canonical with a top-level name that is not UTF-8 is audited, judged and deployed, the first apply lands HEAD as a first seed and leaves the canonical the operator's, the second and third applies and a closing dry-run report 0 changed (the ownership pass runs again after the landing), a hand push of main is refused; a fresh node's dry-run prints one run id, alone and under a deploy-stack-shaped playbook (I-41)
  test_canonical_normalise_as_root.py # C10 / F-CANON, AS ROOT: the ownership pass chowns a root-made canonical to `<operator>:1001` outside hooks/ — every regular file and directory, through its descriptor; a link's own owner, what a link points at outside the tree, and hooks/ stay root's; the second run has nothing to do
  test_content_clone_as_service.py # C10, AS ROOT: the content-clone tasks by ansible-playbook holding a terminal — planted hooks/fsmonitor run as uid 1001 with no group, privilege, terminal or deploy variable; arming/disarming makes the clone again; a clone's mode, a third uid's clone, `local`, the system file, a discard and HEAD reported; an unarmed preview before the configuration file is written compares, and fails where the deploy fails (a local change, a canonical that is no repository), every row with no SUDO_UID (I-33); a parent or storage root the service could write, or a storage root closed to it, refused (and the printed fix opens it); deploy-stack.yml's own storage-root, store and host-dir tasks on a node with no storage root; the restart's condition evaluated
  test_onboard_dryrun.py     # dry-run plans nothing; --apply mutates tmp repo idempotently
  test_search_local.py       # source→record→filter pipeline + --vector filter
  test_gui_api.py            # Flask test client: auth gate (401s) + /api/* passthrough
  test_api.py                # the FastAPI read-only routes via TestClient (service seams mocked)
  test_capture_exception_e2e.py  # the capture-exception add subcommand (dry-run/--commit)
  test_backup_cli_dry_run.py # I-11: backup-configs.yml against a CLI host whose backup is `display current-configuration`, on a local connection — --check --diff passes, says NOT PREVIEWED, records captured and writes nothing; a real run makes the read and fails at that task, which the summary names (needs ansible-playbook, git and ansible.netcommon)
  test_backup_empty_capture_real_run.py # I-5: backup-configs.yml run for real against raw-SSH hosts on a local connection — an empty and a blank answer fail the run and keep their good captures, an unreachable host is kept and not run, the good host's is written and the history's only change, the run id stamped; --check --diff passes and writes nothing; I-10: the Proxmox backup against a host with no Proxmox on it (headers alone) fails the run and keeps its capture with no commit, and its dry-run passes (skipped where pveversion, /etc/network/interfaces or /etc/pve exists); I-62: an unreachable Docker host and Proxmox node are tolerated in a dry and a real run, a Docker host with the compose plugin is captured with its stacks and one without it with an empty stacks section, and one whose daemon is down fails the run by its exit code and keeps its manifest (needs ansible-playbook + git)
  test_run_log_dir_task.py # I-23: deploy-stack.yml's run-log dir task by ansible-playbook on a scratch home — as a host deploy runs it (HOME and USER) and with the launcher's KONTROLL_OPERATOR_*; a fresh home gets ~/.local … runs as the operator's at 0755 (as root, for uid 1000) and a second run changes nothing; an operator's own 0700 ~/.local is kept; a `runs` Docker already made root's is given back while its parents are left (root only); a dry-run reports the directory and makes nothing; inside the installer it is skipped (needs ansible-playbook)
  test_fleet_play_failure_cleans_up.py # I-64: install-node-exporter.yml and wire-logging.yml run for real with a stub role and a stale key — wiring: every host failing, one beside a good one, a refused request (with `_wire_group` from an `-e` and a vars file too), a role that cannot be loaded, a `run_once` failure, a caller's any_errors_fatal over two hosts and over one; install: every host failing, a role that cannot be loaded, unreachable hosts; the key dir is gone wherever the run reaches the cleanup (needs ansible-playbook); I-88: a wiring under `--limit a` (real and --check) and under `--limit g` fails naming the limit, once, one under `--limit a:localhost` wires a alone
  test_backup_failed_run_cleans_up.py # I-9: backup-configs.yml run for real with one raw-SSH host that answers nothing and a stale key in the run's key dir — the run fails at its summary, the history play commits, the key dir is gone, the capture is kept; I-12: one host whose command exits non-zero (named FAILED by the task it stopped at, the key dir gone, its capture kept), and that host beside a good one (the good one captured and committed alone), the same under ANSIBLE_ANY_ERRORS_FATAL=True; I-66: one host whose class is not installed (no such role, a role with no tasks/backup.yml, no device_role), alone (a dry and a real run fail at the summary, which names the entrypoint check; the real run removes the key dir) and beside a good one (captured and committed); one whose class's backup includes a missing file, alone (failed in the rescue, no summary line, the keys kept: the residue) and beside a good one (needs ansible-playbook + git)
  test_backup_limited_run.py # I-90: backup-configs.yml run for real under a group's, a host's and `all`'s --limit (the captures committed once, on the controller for the first limited host; each host's own interpreter does not exist), unlimited (on localhost, as before), with a retention window (pruned) and with no capture dir (refused)
  test_refuse_missing_entrypoint_runs.py # I-66: tasks/refuse-missing-entrypoint.yml run beside a real include_role of `{{ device_role }}` with tasks_from: backup — the check passes where the include runs the entry and refuses where it does not, for each place ansible-core searches in its order (the playbook's roles/, each roles_path entry from ansible.cfg or a relative ANSIBLE_ROLES_PATH, the playbook dir, the name as a path; a roles_path entry ahead of the last two), each entry file name, each way a class has no entry (no role, no entry, no tasks/, an entry that is a directory (a directory of an earlier name is passed over), a role shadowed by an earlier one, by a file or past a dangling link or a link loop, no, an empty or a non-string device_role, a dotted name that is no collection name), an entry indented throughout, each way an entry holds no task (empty, comments only) or cannot be read (mode 0), an entry a YAML or template read would trip on, and each path ansible-core resolves first (`~`, `$VAR`, `..`, a trailing slash); the refusal names the host, the class and the roles path; what the check leaves to the include has rows of its own (a class in collection form, installed or not or with no entry; an entry that does not parse, is not a list or holds `[]`; defaults that do not parse; a dependency not installed) (needs ansible-playbook; validate.sh's no-skip step)
  test_ping_refused_class_runs.py # I-83: ping.yml run for real (local hosts at 127.0.0.1, at a port the row listens on) — a host of each kind the check refuses (no such role, no tasks/check.yml, no tasks/, a check with comments only, no or an empty device_role), alone: a dry and a real run fail with rc 2, the host skips the probe, the summary names it REFUSED and the last play fails it after the key cleanup, which removes the key dir; beside a reachable host (OK, its check run) and an unreachable one (UNREACHABLE, rescued, not failed); a run with no refused class passes with an unreachable host; a class whose check includes a missing file fails in the probe's rescue, alone (no summary, the keys kept: the residue) and beside a reachable host (OK, the key dir gone); under ANSIBLE_ANY_ERRORS_FATAL, a reachable host beside a down or a refused one is still OK and the key dir gone (needs ansible-playbook; validate.sh's no-skip step)
  test_ping_check_failed_runs.py # I-84/I-85: ping.yml run for real (local hosts at 127.0.0.1) with a host that answers and passes, one that does not answer, one that answers and whose check is a `fail`, and one of that class that does not answer — a real run fails with rc 2 for the failing check alone (CHECK FAILED naming the task, never the device's answer; the down hosts UNREACHABLE, rescued, not failed; the last play after the key cleanup; the key dir gone); a dry run passes, each line ending `(dry run: the port was not probed)`; under `--limit` alone, bare, with the require var `false` and with galaxy.py's own VERIFY_PING arguments (`-e kontroll_ping_require_reachable=true`) and VERIFY_ENV, only a host that answered and passed passes the verify; a dry run given the require var is refused, every host failed by name after the key cleanup (`false`: it passes); and a verify that read no inventory passes bare (rc 0, no line) and fails under VERIFY_ENV (rc 1, no play run) (needs ansible-playbook)
  test_g9_age_key_gate_real_stat.py # I-22, AS ROOT for the rows of uid or gid 1001: deploy-stack.yml's own age-key stat, forget and assert, by ansible-playbook, on each shape the path can take — a key uid 1001 can read passes (a 0644 file, a link to one, a 0600 key uid 1001 owns, a root:1001 0640 key); a missing path, Docker's directory, a directory a key was installed into, a pipe, an empty file, a 0600 key of another uid, a link to one, a link to nothing, a 1001-owned 0044 key and a root:1001 0604 key are refused, each in its own words, and a refused key drops the GUI's config record (I-31: a passed one keeps it; in `--check` too); the stat follows the link, computes no checksum, and gives every field the unit's hand-written results do (needs ansible-playbook)
  test_installer_leaves_no_bytecode.py # I-54: installer.yaml sets PYTHONDONTWRITEBYTECODE as a literal, which neither launcher nor the installer's entrypoint names (so none passes or drops it); deploy-stack's lockfile task, which runs under become, runs `python3 -B`; bootstrap.yml's own lockfile task and a task applying a repo filter, run by ansible-playbook over a copy of the tree with that value, leave no `__pycache__` or `.pyc` in it, and without it leave one under scripts/kontroll/ and ansible/filter_plugins/ (needs ansible-playbook); a launcher whose first line runs Python with `-E` or `-I` (pipx's, on CI's runner image), which ignore every PYTHON* variable, runs through its interpreter without the flag
  test_tls_owner_dry_run.py  # I-27: the self-signed TLS tasks by ansible-playbook — a dry-run before the certs are made passes, skips owning them and says so (with a canonical or without); with them there it previews owning them; an apply (AS ROOT) makes and owns them, idempotently; in byo_proxy/acme every TLS task is skipped, dry-run and apply, and an operator's own files are left as they are (needs ansible-playbook; the apply row needs root + openssl, so `validate.sh`'s as-root step runs the file, and a row pins it there)
  test_up_services_empty.py # I-32: deploy-stack.yml's stack_services refusal, compute-the-list, note and `up` tasks by ansible-playbook with a stand-in docker on PATH — an empty stack_services, or only snmp-exporter without its credentials, calls docker never and prints the note with its reason (a bare `up -d` is every service); a list with a service, or one only caddy fills, is brought up as computed and the note is skipped; a string (by `-e` key=value too), a mapping, a name that is not a string, a blank name and one with spaces in or around it are refused, docker never called (needs ansible-playbook)
  test_reload_observability_runs.py # I-8: reload-observability.yml run against a loopback stand-in for Prometheus — --check --diff sends nothing, says the reload was not sent and passes; a real run POSTs /-/reload once and reports the status; a refused reload (500) fails the run, and so does a Prometheus not listening; the run id stamped, on an inventory with no hosts too, once, before the reload's POST task (needs ansible-playbook)
  test_backend_api_basic_auth_runs.py # I-16: roles/backend_api's check and backup for basic-auth hosts by ansible-playbook against a loopback HTTPS stand-in — the pair resolved from the recipe's names, the per-host ones, both, or the per-host ones under empty recipe vars authenticates; no pair is refused by name with no request; a wrong one gets the 401 hint; --check sends nothing; captures only for a good pair; a per-host value that cannot be read beside a good recipe pair authenticates; a pair whose lookup fails is refused by its rescue's message; no credential, config or "censored" printed but the rescued host's own fatal, nor a credential at -v (needs ansible-playbook and openssl)
  test_run_id_stamp_runs.py # I-15: _log-run-id.yml run by ansible-playbook — an inventory with no hosts is stamped once, on the implicit localhost, with the UTC time of the run (under a TZ off UTC); a fleet once, on its first host, its recap gaining no localhost; a `--limit <host>` on that host; an injected `-e kontroll_run_id` is the id printed, on no hosts and on a fleet, and an empty one falls back to a UTC id; a fleet whose first host's connection plugin is not installed is stamped on the controller and the run passes (I-52); I-41: an entry playbook importing the real stamp, and an outer one importing it after its own stamp (deploy-stack's shape), print one id with no hosts, two, `--limit` to one, and a first host whose plugin is not installed, a caller's `-e` id is the one printed, and a `--tags` run prints one (needs ansible-playbook)
  test_bootstrap_install_change.py # I-3: bootstrap's install task's `changed_when` and the play's `in_container`, by ansible-playbook around a stand-in wrapper (needs ansible-playbook) — an install is a change on the node and none in the installer container, a first L2b digest record a change on either, nothing installed none; and, from the YAML alone, the task is bootstrap's own (no `when` or `vars` of its own, nothing else sets `in_container`) and the installer service still sets KONTROLL_IN_CONTAINER=1
  test_journal_dir_choice.py # I-25: deploy-stack.yml's choice of the host journal Vector binds, its four tasks run by ansible-playbook against a stand-in for /var/log/journal — kontroll_journal_dir over the launcher's value (in the installer) over a stat (on the host); a directory or a link to one reads persistent, a file or nothing volatile; the installer with no launcher binds the volatile journal and says NOT KNOWN; any other value is refused, and so is kontroll_journal_dir naming a /var/log/journal the host lacks; the four tasks stand together before the .env render, the only ones setting the choice; I-60: a stack without Vector chooses (the installer with no launcher too), says so and refuses as one with it, saying why, a run with Vector never says it has none, no journal or GID task waits for Vector, and the deploy's `up` is given the journal and GID .env renders (a shell's export would outrank the file); in a dry-run and an apply (needs ansible-playbook)
  test_installer_storage_root.py # I-26: deploy-stack.yml's storage-root tasks by ansible-playbook, with and without the installer's KONTROLL_INSTALLER_STORAGE_ROOT, the operator's -e and inventory vars: the bound root is the default inside the installer, another root or a store outside it (each knob, a `..` or a link that leads out) is refused by name, nothing changes off the installer, the run names its root and its source; plus the refusals' place before anything is made and installer.yaml passing the root as it binds it
  test_no_log_censor_runs.py # I-75: the script on a copy of the ansible-core this run's subprocesses import (`python -I`; skipped where there is none, as in CI's pytest legs, so the repo's own ansible/ cannot stand in), first on PYTHONPATH — after it a failing no_log `fail`, `assert`, `command` and `slurp`, alone, rescued and ignored, print nothing they were given, in the output or the ansible log, where before (on a core that keeps `exception`) they printed it; an ordinary failure still prints; the rewritten file carries the notice; a second run writes nothing; a censor of another shape is refused unwritten; a core that prints the value by another path fails the check; stale unchecked bytecode files (the plain one, the `-O` one) are replaced or removed; as root, a copy uid 1001 owns stays 1001's and the lookup leaves no other cache
tests/e2e/           # GUI in a headless browser over the REAL booted Flask app (opt-in: e2e marker)
  conftest.py                # boots gui/app.py in a thread, service seams mocked, http_credentials
  test_onboard_flow.py       # search → result card → fill form → dry-run → plan renders (Playwright)
  test_paint_rule.py         # C19 in a browser: the panels and dialogs that paint foreign values show a hostile one as text
  test_script_policy.py      # C19's second layer in a browser: injected markup runs nothing; every header button still works
```

```bash
# tests/requirements-dev.txt: pytest + pytest-cov, PyYAML, flask, FastAPI + httpx, cryptography, pytest-playwright
pip install -r tests/requirements-dev.txt
pytest -m "not e2e and not slow"                   # the hermetic default (CI runs this)
pytest --cov=scripts --cov=gui --cov=api --cov-report=term-missing
```

`validate` runs this as its L2 step automatically when `pytest` is installed (it
skips gracefully on a box without it); CI runs it as a dedicated matrix job
(py3.11–3.14). `e2e`/`slow` are excluded from the default — the live lab and any
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

### Age recipients (`tests/unit/_age_recipient.py`)

kontroll-init takes a break-glass recipient only as an X25519 one (bech32 checksum, 32 bytes,
zero padding bits, not a low-order point; a plugin or post-quantum recipient is refused), so a
test that passes one needs a real-length key, and a real-length key written into the source is
what the leak guard flags. `bech32()` encodes bytes under any human part (`pad=` sets the
padding bits age refuses); `x25519_recipient()` builds one as age-keygen prints it, at import,
from fixed bytes. The low-order check is held to X25519 itself by the `cryptography` package
(`tests/requirements-dev.txt`), which `test_the_low_order_check_agrees_with_x25519_itself`
imports plainly, so it fails rather than skips where the package is missing.

## How a build's shell is read (`tests/unit/_build_read.py`)

It also holds the Dockerfile readers (`dockerfile_lines`, `instructions`, `unwrapped`) that
`test_image_pins.py` and `test_no_log_censor.py` both use, so no test module imports another.

`test_image_pins.py` (a Dockerfile `RUN`) and `test_ci_workflows.py` (a workflow step's `run:`) both
hold that a check the build runs — a `pip check`, a `sha256sum -c`, a `pip-audit` — can fail it.
They share one reading of when it can: `commands()` splits the text on `&&`, `||`, `;`, `|` and a
lone `&`, and `gate_faults()` names a check that a later `|| true` or `; cmd`, a pipe, a `&` (the
background), an `if`/`elif`/`while`/`until` condition or a `!` keeps from failing it; that runs
inside a `$(…)`, backquotes or a quoted string, after an ANSI-C `$'…'` or in a list holding one;
that runs in a compound behind `||` or `!`; or that shares its text with a function or alias
definition (or a `hash` or `enable` entry), a `trap`, `exec`, `return`, an `exit` that can be 0, or
a `set -n` or `set -t` (or a `shopt` naming noexec or onecmd, or a `set` or `shopt` that can be
one). It reads each command as written and in the views bash's expansion can turn it into: its brace
expansions expanded and a parameter's default, alternative, assigned default or pattern replacement
read as its word (`${x:-exit} 0`, `${0:+exit} 0`, `${x:=exit} 0`, `${0/*/exit} 0`), then every
expansion removed, read once as empty and once as a space, all but `$#`, `$?` and `$$`, which never
come out empty (`exit$x 0` runs `exit 0` where `x` is unset, `exit${IFS}0` runs `exit 0`), each
parameter given an operator read unset and set empty in every combination (`_choices`, at most
`_CHOICES` readings, past which the command is refused: `exit${x:+foo} 0`, `${y-ex}${x:+foo}it 0`),
a replacement's word also read beside a space (`se${IFS/#/t}-n`), each gap an expansion leaves glued
to a word on both sides read as empty and as a space apart from the others (`ex${x}it${IFS}0`), and
a `$-` anywhere read as the option letters it holds (`set -e; $-xit 0`); expansions are paired as sh
pairs them, so a `)` or `}` that a quote, an escape, a case pattern (its words quoted or escaped
too) or an inner substitution holds closes nothing (`e${x:+$(echo })}xit 0`), a case pattern it
cannot read refuses the command (`e$(case a in $(echo ')')) :;; esac)xit 0`: the readers fail
closed; an `esac` counts only where a command starts (after `;` or a lone `&`), at its paren's own
level, so a decoy one in an argument, a pattern, a `${…}`, a `$[…]`, a redirect's word (`>&esac`) or
a `[[ … ]]` evens nothing; a bash `$[…]` is a frame in one reading and plain text in the other (dash
reads no `$[`), and the command is refused if either reading closes early (`e$(: $[ ; case …`), a
quote inside a `$[…]` a frame of its own; the walk is not sh's parser, a limit test_image_pins.py's
docstring names), a bare `{` inside a `${…}` is a character, and a `'` inside a double-quoted `${…}`
is read both as a quote and as a character (POSIX sh reads it as one); a command that opens an
expansion `commands()` cut at an operator inside it is read with the pieces cut from it (`exit$(:|:)
0`); a brace list it cannot read (an item holding a nested substitution or parameter, or one an
operator inside a quote or a substitution cuts) is refused. It reads errexit the way `/bin/sh` does:
a `;` after `set -e` counts only for a check that starts its AND-OR list and is the whole of it (a
guard before it can skip it), and never inside a compound (a brace group, a subshell, an `if` body,
a loop); a `set -e` counts only as a whole top-level command, and a `set +e`, a `set` or `shopt`
given a variable, a backquoted command, a glob or a brace expansion, or a `shopt` naming errexit,
anywhere, switches errexit off. It removes quoted strings (escapes, a `'` or backquotes inside
`"…"`) when it decides what a check runs inside, but it splits on `&&`, `||`, `;` and `|` inside
quotes too (a lone `&` only outside them). A Dockerfile's lines reach it joined as BuildKit joins
them (no space at a continuation, a double backslash continuing nothing, a byte-order mark dropped,
a whitespace character other than a space, a tab or a line feed refused), an `ONBUILD` read as the
instruction it holds, a `RUN` flag read as part of its first command (and refused in a Dockerfile
that installs with pip, a cache mount at pip's cache too: a mount can put a `pip.conf` or a planted
wheel in front of the install, and the check reads none); a step's `run:` is read whole (a quote or
a backquote spans a newline, and the step is refused when one does or is left open, or when a `'`
sits inside a double-quoted `${…}`, which bash pairs as a quote and sh reads as a character; a `#`
that opens a word outside a `${…}`, or inside a `$(…)` one holds, cuts to the line end, and one
right after a `)` is refused; a C0 control character that is not whitespace is refused) and its
lines joined with `;`, or with a space after one that ends in `&&`, `||` or `|`. It refuses the
shapes it knows it cannot read; the module's docstring gives the rules, and `test_image_pins.py`'s
("What the checks still cannot see") lists the shapes it cannot see and the sound shapes it refuses.
A new check on a build's shell text calls `gate_faults()` rather than reading the operators itself,
so the two tests cannot drift apart.

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
