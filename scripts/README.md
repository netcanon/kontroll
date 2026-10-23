# scripts — operational helpers

Codified one-off / setup operations that aren't Ansible plays or compose files.
Each script is idempotent and reproducible (the cold-agent bar).

## `install-prereqs.sh` / `kontroll-installer.sh` / `host-systemd.sh` — compose-native install (Phase 1)

`sudo bash scripts/install-prereqs.sh` installs the host floor — now just **Docker + compose + git** (the
ansible/sops/age toolchain moved into the ephemeral installer image; `--with-host-ansible` restores the legacy host
toolchain for rollback). `scripts/kontroll-installer.sh <verb>` is then the single entry point: it injects the
operator identity (MF-1) + ensures the host bind targets exist, then runs the installer container
([docker/services/installer.yaml](../docker/services/installer.yaml)) which executes the unchanged
bootstrap/local-canonical/deploy-stack playbooks. Verbs: `fresh-init` · `check` (the `--check --diff` dry-run) ·
*(bare)* `init` · `configure-semaphore` · `shell`. `host-systemd.sh` is the small host-systemd residual
(qemu-guest-agent) the ephemeral container can't own. Full reference + security model:
[docker/installer/README.md](../docker/installer/README.md).

## `make-bundle.sh` / `install.sh` — git-free install

`bash scripts/make-bundle.sh` builds a release bundle — the **tool only**: product code at
HEAD **minus the entire `instance/` overlay** (recipients, secrets, inventory IPs, fleet,
dashboards) + the internal `docs/reviews/` snapshots, shipping the public `instance.example/`
stub → `dist/kontroll-<version>.tar.gz`. `bash scripts/install.sh
<bundle> [dir]` stands a fresh instance up **with no GitHub clone** — `git init` a local
working tree (no remote), then `kontroll-init --fresh` scaffolds your own overlay, then the
existing bootstrap/deploy flow. Replaces the clone
step in [SETUP.md](../docs/SETUP.md) §2; the operator needs no git/GitHub account. See
[docs/local-source-of-truth.md](../docs/local-source-of-truth.md) (Phase C).

## `kontroll.sh` / `make-launch-kit.sh` — bundle-as-compose (C1, published images)

The published-image install path: instead of BUILDING the installer locally, pull it (and the runner/vector) BY DIGEST
from ghcr. `bash scripts/make-launch-kit.sh` (run AFTER a tagged publish + `gen-image-digests.py --refresh
<tag>` + commit) assembles `dist/kontroll-launch-<ver>.tar.gz` — the same instance-stripped tree as `make-bundle.sh`
**plus** `images.env` (the rendered `@sha256:` digest pins) and the `kontroll` launcher (`--allow-unpinned` builds an
unrunnable skeleton for assembly testing). On a fresh node (host floor = **Docker + git only**), `./kontroll <verb>` is
the published-path sibling of `kontroll-installer.sh`: it sources the digest pins, pulls each image by digest
(`docker pull`) and then does a plain `run` (the image is present, so the published one is used and nothing is
rebuilt), and forces `use_published_images=true` for init/check so
deploy-stack pulls the runner/vector by digest too. It lives at `scripts/kontroll.sh` (not `scripts/kontroll`) because
`scripts/kontroll/` is the Python service package; `make-launch-kit.sh` copies it to the kit root as `kontroll`. See
[docker/README.md](../docker/README.md) § Publishing images.

`make-bundle-airgap.sh` is the OFFLINE variant (report 22 §6.3): it pulls every kontroll image by digest, `docker
save`s them into one tar, and bundles the launch kit → `dist/kontroll-airgap-<ver>-<arch>.tar.gz`. The disconnected
node does `docker load -i …` then `./kontroll fresh-init …` with **zero registry reachability** — the digest-pin
survives save/load, and the `kontroll` launcher skips the pull when the installer image is already loaded.

## `update.sh` — non-destructive code update

`bash scripts/update.sh [remote [branch]]` (default `origin main`). Pulls product-code
updates and **merges** them, **preserving instance state** (onboarded hosts, secrets,
fleet selection, dashboards) — the instance is the source of truth, so we never
`git reset --hard`. Code and state live in disjoint files
([config/state-manifest.yml](../config/state-manifest.yml)), so the merge is normally
conflict-free; a conflict means a *code* file was edited locally (surfaced, never lost).
Then re-seeds the local canonical the running instance reads. See
[docs/local-source-of-truth.md](../docs/local-source-of-truth.md) (Phase B). The git-free
operator update (code via the image / a bundle) is Phase C.

## `update-from-bundle.sh` — non-destructive update for a REMOTELESS box

`bash scripts/update-from-bundle.sh <bundle-file> [branch]` (default branch `main`). The
sibling of `update.sh` for a production box with **no upstream git remote** (the
local-as-truth posture): it can't `fetch origin`, so the developer ships a `git bundle` of
the new product code built on a connected machine (`git bundle create kontroll.bundle
<box-HEAD>..main`), `scp`s it over, and runs this. Everything past the fetch is **identical**
to `update.sh` — the same state-preserving `git merge` (conflicts surface a locally-edited
*code* file, never instance state) + the same local-canonical re-seed. Codifies the #145
bundle-merge sync (previously hand-run). See
[docs/local-source-of-truth.md](../docs/local-source-of-truth.md) (Phase B / remoteless).

## `state-snapshot.sh` / `state-restore.sh` — non-git DR

`bash scripts/state-snapshot.sh [out-dir]` tars **exactly** the
[config/state-manifest.yml](../config/state-manifest.yml) paths (secrets, inventory
drop-ins + group_vars, fleet selection, API recipes, dashboards) into a portable
archive — restore anywhere with **no git** (`state-restore.sh <archive>` lays it back
over a fresh base: code ⊕ state). The DR owner now that the instance, not GitHub, is the
source of truth. SOPS-encrypted secrets + inventory IPs inside; the age key is excluded
by design (offline break-glass), so a leaked archive decrypts nothing. See
[docs/local-source-of-truth.md](../docs/local-source-of-truth.md) (Phase C).

## `backup.sh` — optional offsite backup (pluggable remote)

`bash scripts/backup.sh [remote ...]`. Mirrors the instance (code+state) to one or more
**pluggable** git remotes — **opt-in, never required to operate** (the instance is the
source of truth). Targets resolve: explicit args > `instance/instance.yml` `backup_remotes`
> `$KONTROLL_BACKUP_REMOTE` > `backup`. Each target uses its **own single-repo,
strictly-scoped write deploy key** (`IdentitiesOnly`-pinned, generated on the VM) so a
backup remote can touch **only** its own repo — see [SECURITY.md](../SECURITY.md) C7/C8
and [docs/local-source-of-truth.md](../docs/local-source-of-truth.md).

## `run-smoke-gate.sh` — callable live smoke gate

`bash scripts/run-smoke-gate.sh` triggers the fleet liveness gate (TCP probe + each class's
read-only `check` + an honest pass/fail assertion — `ansible/playbooks/ping.yml`, run as the
`ping-fleet` Semaphore template), polls to completion, and reports **PASS/FAIL** with the task
id. Callable post-deploy or from a webhook handler (pass `SEMAPHORE_ADMIN_PASSWORD` in the env
for a remote caller); results stay **queryable** via the Semaphore task API, correlatable by the
`kontroll_run_id` in the output. Exit 0 = PASS. `ping.yml` IS the gate — this formalizes it as a
one-command, pollable check; no duplicate playbook.

## `lib/plaintext-guard.sh` — the content gate in front of a blanket `git add -A`

`sh scripts/lib/plaintext-guard.sh [--tree] <worktree>`. Run (not sourced) just before the commits that take a
whole worktree into the local canonical: the deploy's instance-state commit (`local-canonical.yml`) and the
launch-kit launcher's capture (`kontroll.sh`). `tests/validate.sh` runs it with `--tree` (step `sops-encrypted`).
POSIX sh plus git and the POSIX base tools (find, awk, tr, wc, cat) — the launch-kit host has no python and no
sops; its tests run under every awk implementation they find installed.

| Rule | Refuses |
|---|---|
| `not-sops-encrypted` | a `*.sops.yml` / `*.sops.yaml` (not the `.sops.yaml` config) in which a value does not have the shape of ciphertext — before the `sops:` block, after it, or inside it (a line that is not one of sops's metadata keys, anything indented that does not stand under a list of recipient entries, anything after `version:`) — or that has no such block |
| `secrets-dir-member` | anything under `instance/secrets/` or `ansible/secrets/` that is not `*.sops.yml`, `*.sops.yaml`, `README.md` or `.gitkeep` |
| `private-key-material` | any candidate that holds an age secret key (anywhere in a line), a line that begins with a PEM / PGP private-key header, or such a header anywhere in a line with key material directly after it (one literal `\n` in a string, or the next line of a block scalar). An inlined armored block whose body starts after a blank line or armor header lines — a PGP private-key block — is not seen |
| `unusual-file-name` | in the default mode, a candidate whose name holds a line break |

Default mode reads what a blanket add would commit (untracked and not ignored, modified, already staged), and
`<worktree>` must be the top of its repository; `--tree` reads every file under the secrets directories and needs
no git. Exit `0` nothing to refuse · `3` findings, one line each on stderr
(`plaintext-guard: REFUSED <path> — <rule>[ (line N)]`, never content) · `2` it could not look, with one sentence
saying why (git cannot read the directory as a worktree, it is not the top of its repository, the list of files
did not reach the checker whole, the checker did not finish). **Every caller treats any non-zero status as a refusal.** Read-only. The predicate
assumes whole-file encryption (the shipped `.sops.yaml`) and knows the line shapes the pinned sops writes;
`tests/integration/test_plaintext_guard_sops.py` asks the installed sops. What it does not read is listed in the
script's header and in `SECURITY.md` C1.

## `lib/run-log.sh` — persistent run-logs (Layer 0)

Sourced fail-soft by the operator scripts (`update`/`backup`/`state-snapshot`/`state-restore`/
`run-smoke-gate`); `run_log_init <name>` tees a run's stdout+stderr to a timestamped, 30-day-pruned
log under `${XDG_STATE_HOME:-~/.local/state}/kontroll/runs` — a persistent "what ran, when,
succeeded?" trail (operator-owned, not the uid-1001 runner). See
[docs/logging-architecture.md](../docs/logging-architecture.md) §3.

## `stale-config.py` — which services must restart for their config

`deploy-stack.yml` runs it after `up`. A service that reads a bound config only when it starts (Vector, Grafana's
provisioning, Loki, Prometheus, the exporters, Caddy) keeps that config until it restarts, and `up` recreates a
container only when its service definition changed. The script reads the stack as `docker compose config` resolves
it (every include and interpolation, as the deploy's `up` reads them) and takes, for each service named, its
read-only binds whose source is in the tree, or is a **file** under the storage root (what the deploy puts there:
the Caddyfile, Vector's key, CA bundle and known_hosts, the GUI's age key); a source under both roots is the deeper
one's, so a storage root inside the checkout still holds stores. A bind whose target the service lists in its
`kontroll.config.live` label is one it reloads by itself (Grafana's dashboards, Loki's overrides, Prometheus's
file_sd targets) and is left out. The digest is of the paths, types and content, never the mtime (the generators
rewrite their files every run); a link inside a source is recorded by its target, not followed. A service named
that the resolved stack does not hold (one behind a compose profile) fails the run, by name.

It names each service whose digest differs from what it last loaded, except one whose container `up` printed
`Started` for in this run (`--started`): that one loaded the config as it is. The deploy restarts those named, then
`--write` merges the new digests into a root-only (`0600`) stamp, `<storage root>/config-loaded.json` — a service
a subset deploy left out keeps its record. It prints names only, never a digest: one of the rendered `snmp.yml`
would test guesses at its credentials. A service with no such bind is never named; one missing from the stamp is.

## `configure-semaphore.py`

Declaratively configures the deployed Semaphore so it can run the kontroll
playbooks. Get-or-create by name, so it's safe to re-run.

Creates, in project **kontroll**:
- **Key Store** — `ansible-ssh` (device SSH), `none`, and `github-deploy` when
  `~/.ssh/kontroll_deploy` exists (an offsite remote; skipped otherwise).
- **Repository** — the local canonical, `file:///srv/kontroll.git` @ `main` (no remote needed).
- **Environment** — runtime env (`ANSIBLE_HOST_KEY_CHECKING`, `ANSIBLE_ROLES_PATH=ansible/roles`,
  `ANSIBLE_COLLECTIONS_PATH=/usr/share/ansible/collections`) + the scoped
  **`SOPS_AGE_KEY`** injected as an encrypted secret (lets the runner decrypt the
  `network`/`proxmox` SOPS domains — and only those, see `.sops.yaml`).
- **Inventory** — `instance/inventory` (directory; drop-in `onboarded-*.yml` host
  files are picked up additively), device SSH key attached.
- **Templates** — one per file under `config/semaphore/templates/`.
- **Schedules** — generated per class into `config/semaphore/schedules.generated.yml` by `gen-backup.py`.

Why env overrides (not the repo `ansible.cfg`): Semaphore runs from the repo
**root**, but `ansible/ansible.cfg` uses paths relative to `ansible/`. The few
settings that matter are set as process env so they resolve from the root; the
collections live at the image's baked system path.

### Run (on the control node)

`scripts/kontroll-installer.sh configure-semaphore` (compose-native), or `python3 scripts/configure-semaphore.py` on
a host with the legacy toolchain — after the deploy, because it talks to the running Semaphore. The admin password
is read from the `semaphore` SOPS domain with the control node's age key; no export is needed
(`SEMAPHORE_ADMIN_PASSWORD` in the environment overrides it).

Reads key material from `~/.ssh/ansible_ed25519` and, when they exist, `~/.ssh/kontroll_deploy` and
`~/.config/semaphore/age.key` (the scoped key is minted after the deploy, in the GUI's Keys dialog — run this
again then).

## `galaxy.py` — capability-aware onboarding search

Phase 1 of the capability-matrix design ([docs/capability-matrix.md](../docs/capability-matrix.md)).
Searches **Ansible Galaxy + locally-installed collections** in one query, labelling
each result by the drop-in capability **vectors** (`vectors/*.yml`: actuate / backup
/ bespoke) — derived by probing, not hand-maintained.

**Architecture:** `galaxy.py` is a **thin CLI** — argparse + `cmd_*` formatters — over the
`scripts/kontroll/` **service package** (core modules `paths`/`predicate`/`probe`/`catalog`/
`record`/`gitio` + one `service/<domain>.py` per verb). The `cmd_*` wrappers call service
functions that return structured data and render it; the same service layer backs the planned
API ([docs/api-architecture.md](../docs/api-architecture.md) §1). Patch test seams in their home
modules (`kontroll.probe`/`kontroll.catalog`/`kontroll.gitio`/`kontroll.paths`), not on `galaxy`.

```bash
python3 scripts/galaxy.py search arista              # galaxy + local, capability-labelled (+ suggested backend)
python3 scripts/galaxy.py search mikrotik --vector backup   # only backup-capable results
python3 scripts/galaxy.py probe cisco.ios [--json]   # deep-probe one collection (record)
python3 scripts/galaxy.py classify cisco.ios         # which backend fits (`best`: onboard's pick for a new class)
python3 scripts/galaxy.py scaffold arista.eos --key arista_eos --group core_switch [--dry-run]
                                                     # generate modules/<key>/module.yml (staged)
#   --key, --group, --secrets and --backend follow the onboard's rules for a name (and --group is not `all`,
#   `ungrouped` or `localhost`); the file is written under the write root, like every other module write, and
#   never beside a class the tool ships under the same key. Without --secrets the class gets the backend's
#   `default_secrets_domain`; `onboard` under the key of a class that already exists (one the tool ships, or one
#   onboarded before) stores in that class's own domain, whether --secrets is given or not
python3 scripts/galaxy.py onboard arista.eos --key arista_sw --group core_switch --host 192.0.2.250
                                                     # dry-run plan: module + drop-in inventory host + fleet
python3 scripts/galaxy.py onboard arista.eos --key arista_sw --group core_switch --host 192.0.2.250 \
        --username admin --password '***' \
        --apply --commit --push --bootstrap          # files + creds→SOPS + commit + push + install + verify
python3 scripts/galaxy.py openapi https://host/api/v3/openapi.json --name radarr --emit
                                                     # derive an api-backend recipe from an OpenAPI spec
#   --name is one path component (lower-case letters, digits, `_`, `-`); without it the name is derived from the
#   specification's title
python3 scripts/galaxy.py refresh                    # cache the derived matrix (gitignored)
python3 scripts/galaxy.py audit                      # enabled-module collections vs installed
python3 scripts/galaxy.py capture-exception add --subject fortigate --match 'FortiGate_*' \
        --behavior non_deterministic --commit        # declare a misbehaving capture (matrix); --push for origin
```

**`openapi` — the API tail (recipe authorship, automated).** Fetches an
OpenAPI/Swagger spec (a live instance's `/openapi.json`, an apis.guru entry, or a
vendor file), reads its `securitySchemes` → auth, `servers` → port/base, and
classifies endpoints by heuristic (GET `/backup|/export|/config` → backup; any
POST/PUT/PATCH → actuate; GET `/status|/health|/ping` → check) into an
`ansible/backends/api/recipes/<name>.yml` for the [`backend_api`](../ansible/roles/backend_api/README.md)
role. The recipe is **staged** — token vars are named (you wire them to SOPS), and
the endpoint→capability mapping is heuristic, so live-verify before relying on it.
Guessed auth and unmatched capabilities are flagged (`[!]`, `✗`).

Each result is a structured **record** (`--json`): metadata + a `capabilities`
block keyed by vector + a `suggested_backend` (the backend `onboard` would choose: the first match whose
registry entry declares a `role:`; `backend_candidates` keeps every match — none at all for a collection that
carries no signal a backend classifies on: `raw_ssh` is never auto-matched, S24). A plain SSH host has no
collection to find; a search for `ssh`, `linux`, `server`, … returns the **Generic SSH host** card
(`ansible.builtin`, backend `raw_ssh`), and `onboard ansible.builtin --backend raw_ssh` pins no collection for it.
An onboard or scaffold that no backend fits exits with what the collection showed and the remedy (`--backend
raw_ssh` only if the target is an SSH host). The pipeline is **search → classify
→ scaffold → onboard**: find a collection, see what it can do + which backend drives
it, emit a device-class declaration, and stand the host up. Backends are drop-in
(`ansible/backends/<name>/`); overrides correct the residual (`overrides/`).

**`onboard` is the one-shot.** Dry-run prints the plan; the flags actuate, each implying
the prior:
- `--apply` — the repo mutations, idempotent: the module declaration
  (`modules/<key>/module.yml`), an **additive, self-contained drop-in inventory host**
  (`instance/inventory/onboarded-<key>.yml` — connection vars from the backend + **inline
  per-host SOPS cred lookups**, so no `group_vars` hand-edit even for a brand-new group),
  and the `instance/fleet.yml` enable.
- `--username/--password` or `--api-token` — **encrypted into the secrets domain**
  (`sops --set`, namespaced per host) at apply time; the host references them inline.
- `--commit` — rationale-first commit (plus the one configurable attribution trailer: the CLI reads
  `KONTROLL_COMMIT_TRAILER` from the calling shell — the containers take it from `docker/.env`, see
  [`docker/.env.example`](../docker/.env.example)), including the
  re-encrypted secrets file, then **lands on the local canonical** (`/srv/kontroll.git` — `gitio.land_main`:
  judged by the promote gate first, a pack on a pipe, one compare-and-swap; never a `git push`, which the
  canonical's hook refuses for `main` from everyone — SECURITY.md C10) so **Semaphore sees the new host with no
  GitHub** (the instance is the source of truth — [docs/local-source-of-truth.md](../docs/local-source-of-truth.md)).
  A tree the gate refuses (a fleet line that is not a class key, two units pinning one collection differently)
  stays committed in the checkout and is NOT landed: the printed sentence names what to correct (rc 2).
- `--push` — **optional** offsite backup to GitHub `origin`; not needed to operate. Fails
  clean (the instance is unaffected) if there's no write-capable remote.
- `--bootstrap` — installs the new collection + live-verifies via `ping --limit`.

The **only** inputs are the search selection (the collection) and the connection details
(host + creds) — everything else is automated. This is the engine a GUI drives.

Depth matters: **local** results are labelled DEEP (`ansible-doc -j` — full signals,
incl. whether a `*_config` module exposes `backup:`); **galaxy** results are labelled
SHALLOW (from the Galaxy `contents` list — module names + plugin types). A vector
that can't be confirmed shallow shows `?`; deep-probe a hit before adopting it.
Vectors are drop-in: add `vectors/<name>.yml` and search/probe pick it up.

## See also
- [../docs/capability-matrix.md](../docs/capability-matrix.md) — the design this implements
- [../vectors/](../vectors/) — the capability-vector registry (drop-in)
- [../docs/SETUP.md](../docs/SETUP.md) — first-run + continual config
- [../ansible/playbooks/deploy-stack.yml](../ansible/playbooks/deploy-stack.yml) — installs Docker + builds the runner + brings up the stack
- [../docker/semaphore-runner/Dockerfile](../docker/semaphore-runner/Dockerfile) — the custom runner image
