# QA & Release Pipeline — plan of record

How kontroll gets **full CI** (unit + integration + security), a **test-after-change**
discipline, and — once the GUI alpha matures — an **automated build + public
dissemination** path beyond git. Modeled on the netcanon/NetConfig setup
(the netcanon repository, read-only reference), **adapted** for kontroll's
different shape: kontroll is not one Python package but a *composed control plane* —
Ansible/IaC + a Python tool (`scripts/galaxy.py`) + a Flask GUI (`gui/`) + a multi-
container compose stack. So the pipeline is multi-artifact, and one hard constraint
shapes everything:

> **CI proves OFFLINE correctness; the live lab is never in CI.** Ansible runs that
> touch real devices stay on the control VM / Semaphore (the "L3 live-verify gate").
> CI covers lint, unit, integration-with-mocks, compose validation, and security
> scans — everything that needs no real fleet.

Status legend: ⬜ todo · 🟡 partial (exists, needs lift) · ✅ done.

---

## 0. What already exists (build on these, don't duplicate)

| Layer | Today | File |
|---|---|---|
| 🟡 L1 lint/validate | yamllint + ansible-lint (`moderate`) + ansible syntax-check + `docker compose config` + promtool + **sops-encrypted check** + **secret-grep** | [tests/validate.sh](../tests/validate.sh) / `.ps1` |
| ⬜ Role testing | none automated. Roles are parsed by `--syntax-check`; nothing evaluates them. The Molecule scaffold was removed on 2026-10-12: its converge set a flag that every role answered by skipping its device tasks, so its idempotence step could not fail | [tests/mock-inventory.yml](../tests/mock-inventory.yml) |
| 🟡 Mock seam | `tests/mock-inventory.yml` (RFC-5737 IPs, `connection: local`) + `tests/e2e/` non-human onboarding fixture | [tests/](../tests/) |
| ✅ pytest (code half) | `tests/{unit,integration}` over `galaxy.py` + the GUI, fully offline; in `validate` (L2) + a CI matrix job | [tests/README.md](../tests/README.md) |
| ✅ GitHub Actions | `ci.yml` (validate + pytest **py3.11–3.14**) · `security.yml` (gitleaks history + pip-audit) · **`zizmor.yml` (GHA workflow audit)** · `dependabot.yml`; all checkouts `persist-credentials: false`, hybrid action-pinning policy (`.github/zizmor.yml`) | [../.github/](../.github/) |
| ✅ Secret hygiene | SOPS+age per-domain, `no_log` convention, secrets gitignored, gitleaks gate | [SECURITY.md](../SECURITY.md) |

The remaining gap vs netcanon: **Python static analysis (ruff/mypy/bandit), a check-mode run of
the plays in CI, trivy image scanning, and the signed-image release path.** This plan closes those.

---

## 1. Test framework & layout (🟡 — landed for galaxy.py + gui; schema tests next)

**pytest** is adopted with the netcanon layered layout, scoped to kontroll's real testable
surfaces: `scripts/galaxy.py` (pure logic + subprocess orchestration) and `gui/` (Flask).
The `unit/` + `integration/` layers below are **implemented and green** (fully offline —
see [tests/README.md](../tests/README.md) "Code-half pytest"); **Playwright GUI e2e**
(`tests/e2e/test_onboard_flow.py`, a dedicated CI job) is implemented + green too. The ⬜
items (a check-mode run of the plays in CI, recipe/vector schema tests) remain.

```
tests/
  unit/                      # pure functions, no I/O
    test_predicate_engine.py #   eval_pred / eval_vector truth tables (True/False/None)
    test_classify.py         #   backend classify from synthetic facts
    test_build_record.py     #   record schema + override merge (incl. yes/no→state)
    test_fleet_edit.py       #   enable_in_fleet idempotent insert, comment-preserving
    test_openapi.py          #   derive_auth / derive_server / classify_endpoints heuristics
  integration/               # galaxy.py + gui as black boxes, no live lab
    conftest.py              #   tmp repo fixture (copy modules/ vectors/ backends/)
    test_onboard_dryrun.py   #   onboard dry-run plan content; --apply into a tmp repo; idempotency
    test_search_local.py     #   search over a fixture collection set
    test_gui_api.py          #   Flask test client: /api/search, /api/onboard (dry-run), /api/classify
  e2e/                       # ✅ Playwright over the booted GUI (service seams mocked); a CI job
    test_onboard_flow.py     #   search → fill form → dry-run → assert plan rendered (headless)
  conftest.py                # shared fixtures: sample modules/vectors/backends dirs
  README.md                  # update: pytest layers + the live-lab exclusion
```

**Adopt from netcanon, verbatim-in-spirit:**
- A `FakeCollector`-style seam: the probers/sources (`kontroll.probe.deep_probe`,
  `kontroll.catalog.local_installed`/`galaxy_search`) shell out to `ansible-doc`/`ansible-galaxy`;
  tests patch them in their home modules to inject canned facts (mirrors netcanon patching the single
  `get_collector` entry point) instead of needing ansible installed. ✅ done — they live in the
  `scripts/kontroll/` service package (workstream 1 of docs/api-architecture.md).
- `pytest` markers (`unit`, `integration`, `e2e`, `slow`) + `addopts = -q --strict-markers`.
- Coverage on `scripts/` + `gui/` with `--cov-report=term-missing`.
- Per-layer `conftest.py`; `tmp_path` for isolation; fixtures build a throwaway repo so
  `--apply`/`enable_in_fleet`/`sops_set` never touch the real tree.

**kontroll-specific additions:**
- ⬜ **The plays evaluated in CI** — `ansible-playbook --check` of the install playbooks on a
  fresh checkout and of the fleet plays against `tests/mock-inventory.yml`, so templating,
  role dispatch and `set_fact` logic are executed without the lab. Check mode takes the
  role Molecule was scaffolded for (the scaffold never asserted anything and is gone).
- ⬜ **Recipe/vector/backend schema tests** — assert every `vectors/*.yml`,
  `ansible/backends/*/backend.yml`, `backends/api/recipes/*.yml` parses and matches its
  contract (the extensibility seam — netcanon's "frozen pipeline signatures" analogue).

---

## 2. Linting / static analysis (🟡 → ⬜ add Python)

YAML/Ansible linting exists (`tests/validate`). Add Python static analysis (netcanon
recommends but hasn't wired it — we should, given galaxy.py + gui are real code):
- ⬜ **ruff** (lint + format) over `scripts/` + `gui/` — config in `pyproject.toml`.
- ⬜ **mypy** (gradual typing) on `scripts/galaxy.py` — it's the engine; types pay off.
- ⬜ **shellcheck** on `tests/validate.sh` + any shell.
- Keep ansible-lint; execute the **`moderate`→`safety` graduation** already planned at
  the Phase-2 exit (see `.ansible-lint` GRADUATION PLAN + engineering-standards §1).

---

## 3. Security testing (⬜ — net new, highest QA value)

| Control | Tool | Scope | Notes |
|---|---|---|---|
| ✅ Secret scanning | **gitleaks** (in `tests/validate` + `.pre-commit-config.yaml`) | working tree | `.gitleaks.toml` allowlists SOPS ciphertext + `local/`; custom age-key rule; verified catches a planted age key. *Next:* a CI job + full-history scan |
| ✅ SOPS-encrypted gate | `validate` `sops-encrypted` = `scripts/lib/plaintext-guard.sh --tree` (every value ciphertext, nothing else in the directory, no private key); the same guard gates the commits that take a whole worktree into the canonical | `instance/secrets/*`, and at deploy everything the commit would take | `tests/unit/test_plaintext_guard.py` runs in CI on every pull request; `sops-guard-conformance` holds the predicate against the installed sops, and fails if any of its tests was skipped |
| ⬜ `no_log` enforcement | a pytest/grep gate | every task touching a cred/SOPS value | already a convention + partial grep; make it a hard CI assertion |
| ⬜ Python deps | **pip-audit** | `gui/requirements.txt` + any galaxy deps | netcanon uses Dependabot; add pip-audit too |
| ⬜ Container/image scan | **trivy** | runner image, onboard-gui, homepage/grafana/prom images | scan on build, fail on HIGH/CRITICAL fixable |
| ⬜ Ansible content | **ansible-lint** security profile + **kics/checkov** (optional) | playbooks/roles | infra-as-code misconfig scanning |
| ⬜ SAST (Python) | **bandit** | `scripts/`, `gui/` | the GUI is privileged — worth it |
| ⬜ Dependency updates | **Dependabot** (`.github/dependabot.yml`) | pip, github-actions, docker | copy netcanon's config near-verbatim |
| ⬜ **GUI-specific** | auth + TLS + audit log before beta | `gui/` | tracked in [gui/README.md](../gui/README.md) security posture; the GUI can write the repo, encrypt secrets, push, and run Ansible — treat as a crown-jewel surface |

---

## 4. Test-after-change discipline (⬜)

Mirror netcanon's dev loop, enforced locally + in CI:
- 🟡 **`.pre-commit-config.yaml`** — **gitleaks + detect-private-key landed** (the secret
  guard); still to add: `ruff`, `yamllint`, `ansible-lint`,
  `tests/validate` (fast subset), and `pytest -m "not e2e and not slow"`. Netcanon notes
  pre-commit "should catch most issues before push" — we make it the local gate.
- ⬜ **One entry point**: extend `tests/validate.sh` (or add `make test`) to run, in order,
  L1 (lint/validate) → L2 (`pytest -m "not e2e"`). `validate` stays the single "is it
  green?" command contributors already know.
- ⬜ Document in `CONTRIBUTING.md` (new) + the CLAUDE.md "Before you commit" checklist:
  the required sequence and which layers run where (local vs CI vs live-VM).
- Keep the existing doctrine hooks: Documentation Sync Checklist, access-chain headers,
  `--check --diff` before state-changing plays. These ARE the test-after-change culture
  for the IaC half; the pytest layer adds it for the code half.

---

## 5. CI platform — GitHub Actions (🟡 — as built; the check-mode run and gui-smoke still ⬜)

Five workflows (`.github/workflows/`), the same files in the public repository and the private
instance repository (docs/public-split.md). Every job's `runs-on` is
`${{ fromJSON(vars.KONTROLL_RUNS_ON || '"ubuntu-latest"') }}`: GitHub-hosted by default (the
public repo — a public repository must never run on a self-hosted runner, a fork PR would execute
on it), the org's self-hosted runners where the private instance repo sets the variable.

The four gating workflows run on every push to `main` **and to any `homelab*` branch**: the private
instance repository's permissive tine is a long-lived branch that is never merged to `main`, so it is
gated on push rather than through a pull request (a PR against `main` on a repository without branch
protection is one mis-click from a merge). A public repository has no such branch; the entry is inert.
`publish-images.yml` runs on `v*` tags and dispatch only — never on a branch push.

### `ci.yml` — on `[pull_request, push:{branches:[main, homelab*]}]`, concurrency cancel-in-progress
- **validate**: `tests/validate.sh --strict` — the SAME gate as local + the VM, with every tool it
  can run installed in the job (sha256-pinned sops, gitleaks, promtool, vector; pinned yamllint /
  ansible-core / ansible-lint), so a missing tool is a failure, never a silent skip. Covers yamllint,
  ansible-lint, `--syntax-check`, `docker compose config`, promtool, vector validate, the sops /
  secret-grep / gitleaks gates, every `gen-*.py --check`, the test-docstring and port-binding
  guards, the discovery P-1 fixture gate and the identifier-leak gate (`pii-guard`).
- **tests** (matrix Python 3.11–3.14): `pytest -m "not e2e and not slow"` with coverage; the
  shell-outs are mocked (§1), no ansible, no lab.
- **snmp-modules**: rebuild `modules.generated.yml` with the pinned CGO snmp_exporter generator
  and byte-compare (the no-bespoke gate for the SNMP module).
- **e2e**: the Playwright suite against the in-process Flask app, headless chromium.
- ⬜ still planned: a **check-mode run of the plays** (templating, role dispatch) and a **compose-up smoke** (boot
  the minimal stack, curl the API/GUI health, one onboard dry-run) — the layer the fault ledger
  says would have caught most live faults.

### `pii-guard.yml` — on `[pull_request, push:{branches:[main, homelab*]}]`
- `tests/_leak_guard.py --tree` (structural + instance-token layers; the private list arrives as the
  repository secret `KONTROLL_LEAK_TOKENS`) + the "no tracked `instance/` in a public tree" path
  assertion. Always GitHub-hosted. The required check *No leaked personal identifiers* (SECURITY.md C21).

### `security.yml` — on `[pull_request, push:{branches:[main, homelab*]}, schedule: weekly]`
- gitleaks (full history, 8.30.x) · pip-audit (gui · api · tests deps). ⬜ bandit · trivy (images).

### `zizmor.yml` — on `[pull_request, push:{branches:[main, homelab*]}]` for `.github/` paths + weekly; workflow-security lint, advisory (log-only; plus a SARIF upload to code scanning where the repository variable `KONTROLL_CODE_SCANNING=true`).

### `publish-images.yml` — on `[push: tags 'v*']` + `workflow_dispatch` (see §6)

### `.github/dependabot.yml` — pip (gui + api + tests; the `>=` floors use `increase-if-necessary`, so pure floors get no version PRs), github-actions, docker; weekly, 7-day cooldown,
minor+patch grouped, PR limits (netcanon parity).

> **Enforcement.** On the public repository `main` is governed by a ruleset: pull request required,
> every check above required, no force-push, no deletion; `v*` tags are immutable. On the private
> plan the same checks run but are advisory.

---

## 6. Build & public dissemination (⬜ — after GUI alpha → beta)

kontroll's "product" is the **composed stack**, so dissemination ≠ one wheel. Three
artifact classes, all reproducible, all from tags:

1. **Container images → GHCR** (primary), signed + attested like netcanon:
   - `ghcr.io/netcanon/kontroll-runner` (the Semaphore runner: ansible+sops+age+collections+flask)
   - `ghcr.io/netcanon/kontroll-onboard-gui` (or fold into the runner with a command)
   - Build via `docker/build-push-action@v7`; **cosign keyless** (Sigstore + GH OIDC);
     **SBOM** via Syft (SPDX); `provenance: true`; `cosign verify` post-publish.
   - Tag scheme: semver from git tags (adopt **setuptools_scm**-style or a simple tag→tag map).
2. **A release bundle → GitHub Releases**: the `docker/compose.yaml` + fragments +
   `dashboards/` + `prometheus/` + an install script, zipped, so a third party can
   `git clone || download bundle` and stand up their own kontroll (with their own SOPS
   keys + inventory). This is the "publicly disseminate other than git" ask — a versioned,
   self-contained control-plane bundle.
3. **(Optional) `galaxy.py` as a tiny package** → PyPI via **Trusted Publishing** (OIDC,
   no token), if the onboarding tool earns standalone use. Low priority.

**Versioning & provenance:** git tags drive everything (netcanon uses setuptools_scm with
`no-local-version`). A tag `vX.Y.Z` triggers `release.yml` → build+sign+SBOM+publish images
→ attach the bundle to the Release → mark pre-release for `-rc/-alpha/-beta`.

**Gate:** dissemination starts only once the GUI is **beta** (auth + TLS + audit log) and
the §1–§5 suites are green — we don't publish a privileged onboarding surface before it's
fronted by auth.

---

## 7. Sequenced rollout (the actionable list)

1. ✅ **Testability refactor** in `galaxy.py` — none needed: the three shell-outs
   (`_ad`/`deep_probe`, `local_installed`, `galaxy_search`) are already single-purpose
   module functions, so tests monkeypatch them directly (the "injectable seam" already
   exists); the GUI's service seams are the same (in-process; `run_galaxy` retired). No production code changed.
2. ✅ **pytest scaffold** (`tests/unit` + `tests/integration` + `conftest.py` + markers in
   `pyproject.toml`): predicate-engine + classify + build_record + fleet-edit + openapi +
   onboard-dryrun + search + gui-api tests, all offline. Wired into `tests/validate` as L2
   (skip-if-absent) **and** a dedicated GitHub Actions matrix job (py3.11–3.13).
3. ⬜ **Python static analysis** (ruff + mypy + bandit) + `pyproject.toml` config.
4. 🟡 **`.pre-commit-config.yaml`** (gitleaks + detect-private-key landed) + `CONTRIBUTING.md`.
5. 🟡 **`ci.yml`** — the `validate` gate runs hermetically in GitHub Actions (lint +
   secrets + compose-config + ansible-syntax; collections via `scripts/gen-requirements.py`)
   **+ a `tests` matrix job (py3.11–3.13) running the unit/integration pytest suite**.
   *Next:* the check-mode run + gui-smoke (build the image, curl `/`) as those land.
6. 🟡 **`security.yml`** (gitleaks full-history + pip-audit) **+ `.github/dependabot.yml`**
   (pip + actions + docker) landed. *Next:* bandit + trivy as the GUI/images harden.
7. ⬜ **ansible-lint `moderate`→`safety`** graduation (already-planned Phase-2 exit action).
8. ⬜ **`release.yml`** + image signing/SBOM + the release-bundle job (post GUI-beta).

Items 1–4 are local-dev wins (do first); 5–6 add the CI gate; 7–8 are hardening + ship.

## See also
- [tests/validate.sh](../tests/validate.sh) — the existing L1 gate this extends
- [docs/engineering-standards.md](engineering-standards.md) — lint graduation + the test pyramid
- [gui/README.md](../gui/README.md) — the GUI's security posture (the beta gate for §6)
- [SECURITY.md](../SECURITY.md) — controls the security workflow (§3) enforces
- [api-architecture.md](api-architecture.md) — the API-ification plan whose test layers plug into this pyramid
- [testing-standards.md](testing-standards.md) — the per-test docstring + data-testid discipline this CI gate enforces
- netcanon reference (read-only): the netcanon repository — `.github/workflows/`, `tests/`, `AGENTS.md`
