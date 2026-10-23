# Local source of truth — making git optional for the operator

**Goal:** the *running instance* (the control VM) is the source of truth. git is a
**nice-to-have** — an internal mechanism + an optional offsite backup — never a
mandatory, user-facing dependency. An operator must be able to onboard devices,
schedule jobs, and run the whole control plane **without a GitHub account or any
remote git at all**.

**Decision (locked):** internal git on the VM is acceptable (we keep cheap rollback
history + Semaphore's repository model); what we remove is the dependency on a
**remote** (GitHub) for normal operation. Semaphore reads a **local bare repo** on
the VM, not GitHub.

## The reframe: product vs. instance

The current architecture conflates two things in one git repo and clones both from
GitHub. Splitting them is the whole job:

| | What | Lives in | git for the operator? |
|---|---|---|---|
| **Product / code** | roles, backends, playbooks, `galaxy.py`, `gui/`, built-in modules, `tests/` | the **container image** (versioned, shipped) | no — git is the *developer's* tool |
| **Instance state** | inventory + onboarded hosts, SOPS secrets, `instance/fleet.yml`, group_vars, dashboards, user-derived recipes/modules | a **persistent volume on the VM** (authoritative) | no — optional offsite backup only |

GitOps (GitHub = truth, VM = disposable clone) → **local-state** (VM = truth, git =
optional backup + internal transport).

## Where git is load-bearing today (the coupling to remove)

1. **Semaphore clones GitHub per job** (the hard one) — config changes are invisible until pushed.
2. **`onboard --push` / GUI** must push to origin for Semaphore to see a device.
3. **Work loop** `git reset --hard origin/main` treats the VM as disposable. *(Phase B
   replaces this with the merge-based [scripts/update.sh](../scripts/update.sh) — state-preserving.)*
4. **Secrets/inventory/fleet/modules** live in the repo, riding the same clone.
5. **Bootstrap/deploy** assume a cloned repo.
6. **Reproducibility** is *defined as* "it's in the GitHub repo."

Already local & fine: backups (`/var/lib/kontroll/backups`), age keys (on the VM),
image builds (from the local checkout). Only **canonical-state distribution** is git-bound.

## The gaps (G1–G8)

- **G1 Code/state separation** — a writable state overlay distinct from the read-only
  base in the image, merged at runtime via the existing drop-in dirs (inventory dir,
  `modules/`, `backends/recipes/`, `overrides/`, dashboards). Need: designate state
  paths, precedence, and physically split them out of the disposable checkout.
- **G2 Local execution for Semaphore** — Semaphore reads a **local bare repo**
  (`file:///srv/kontroll.git`, mounted) instead of GitHub. (Validated: the runner's
  git clones `file://` fine.)
- **G3 Persistent authoritative state** — a canonical location that survives redeploys
  and is **never** reset from a remote.
- **G4 Git-optional onboard/GUI** — default = commit-to-local (+ push the local
  canonical); GitHub push demoted to opt-in backup. (Also dissolves the write-key trust
  gate — no remote to push to for normal operation.)
- **G5 Git-free install** — stand up an instance from image + release bundle; scaffold
  base + empty state with no `git clone` from GitHub. (Converges with the QA-plan bundle.)
- **G6 State backup/DR without GitHub** — snapshot/restore the state volume (tar, or the
  **local** git history for rollback) + an *optional* remote that can be **any** target
  (GitHub, another git host, S3, USB) — none required.
- **G7 Re-anchor security** — the read-only deploy key was the write-protection; with
  local-as-truth, protection moves to filesystem perms + which user the GUI/onboard run
  as on the state volume. Simpler, but deliberate (the GUI is privileged).
- **G8 Two-persona clarity** — a config flag (`state.source_of_truth: local` default,
  optional `git_remote:` for backup) + docs separating the kontroll **developer**
  (git/GitHub, normal) from the **operator** (git optional).

## Phased plan

### Phase A — Decouple execution (no GitHub needed to operate)  ← *implementing now*
- A **local bare repo** `/srv/kontroll.git` = the instance's canonical git,
  created + seeded from the working tree, mounted **read-only** into the Semaphore
  container at `/srv/kontroll.git`.
- Semaphore's **Repository → `file:///srv/kontroll.git`** (key `none`); the GitHub
  deploy key is no longer required to operate. (`configure-semaphore.py`.)
- The working tree gains a `local` remote → the bare repo; `onboard`/GUI **land on the
  local canonical** after commit (`gitio.land_main` → `land_commit`: judged by the promote gate, a pack on a pipe,
  one compare-and-swap — never a `git push`, which the canonical's hook refuses for `main` from everyone;
  SECURITY.md C10), so the instance sees the change with **no GitHub**.
- `onboard --push` is repurposed to the **optional** GitHub (offsite backup) step.
- A `local-canonical.yml` play (idempotent) ensures the bare repo exists + lands the
  working tree's HEAD on it (`scripts/kontroll-land.py`, `--check` first under the dry-run; used after a
  developer code-pull; the operator never needs it).

**Phase-A exit:** a Semaphore job (ping/backup) runs end-to-end from the local repo;
onboarding a device + running it requires zero GitHub.

### Phase B — Separate code from state, no-clobber updates (G1)  ← *done (core)*
- **The code/state boundary is now explicit** ([config/state-manifest.yml](../config/state-manifest.yml)):
  state = inventory drop-ins, group_vars, secrets, API recipes, fleet selection,
  dashboards; everything else is product code. They live in **disjoint files**.
- **The instance never `reset --hard` again.** [scripts/update.sh](../scripts/update.sh)
  pulls upstream code and **merges** it, preserving instance state (the merge is
  conflict-free because code & state don't share files; a genuine conflict means a
  *code* file was edited locally — surfaced, never silently lost), then lands the merged
  HEAD on the local canonical through the promote gate (`scripts/kontroll-land.py`; a tree
  the gate refuses stays in the checkout, exit 2, with the sentence). This kills the Phase-A
  caveat (a dev code-push clobbering state).
- **Remoteless boxes** (a production instance with no upstream remote — the whole point
  of this doc) can't `fetch origin`, so [scripts/update-from-bundle.sh](../scripts/update-from-bundle.sh)
  applies a `git bundle` of new product code (built `git bundle create … <box-HEAD>..main`
  on a connected machine, `scp`'d over) and then reuses the **identical** merge + landing
  body. This codifies the #145 bundle-merge sync that was previously hand-run on the
  prod box; the conflict semantics (a *code* file edited locally) are the same as `update.sh`.
- *Deferred to Phase C (it couples with git-free install):* making the base
  (roles/playbooks/galaxy/gui) come **physically from the image** so an operator gets
  code updates with no git at all. Today code rides the canonical repo; the merge keeps
  it safe. The manifest is exactly the set Phase C's install/restore reconstitutes.

### Phase C — Git-free install + state backup (G5, G6)  ← *pluggable remote done*
- **Pluggable optional offsite remote — done.** [scripts/backup.sh](../scripts/backup.sh)
  mirrors the instance (code+state) to any configured git remote; opt-in, never required
  to operate. Each target gets its **own single-repo, strictly-scoped write credential** —
  a GitHub deploy key bound to exactly that repo, `IdentitiesOnly`-pinned, generated on
  the VM. Provisioned + **verified** against `netcanon/kontroll-prod-test` (an example
  user/offsite target): the key authenticates only as that repo and is **denied** on
  every other repo (SECURITY.md C7).
- **Non-git state snapshot — done.** [scripts/state-snapshot.sh](../scripts/state-snapshot.sh)
  tars exactly the `state-manifest.yml` paths into a portable archive (restore anywhere,
  no git) and [scripts/state-restore.sh](../scripts/state-restore.sh) lays it back over a
  fresh base. **Verified:** the archive holds only state (secrets/inventory/fleet/recipes/
  dashboards) — **zero code** — and round-trips clean. No age key is included by design
  (a restore brings the control key's off-box copy, or the offline break-glass key —
  [break-glass.md](break-glass.md)), so a leaked snapshot alone decrypts nothing.
- **Git-free install/update — done.** [scripts/make-bundle.sh](../scripts/make-bundle.sh)
  builds a release bundle (product code at HEAD, **minus the entire `instance/` overlay** +
  internal `docs/reviews/` snapshots — it ships the `instance.example/` stub only);
  [scripts/install.sh](../scripts/install.sh) stands a fresh instance up **from the
  bundle with no GitHub clone** — `git init` a local working tree (no remote), seed the
  canonical, then the existing bootstrap/deploy flow. **Verified:** the bundle is clean
  (no secrets, no recipients, no instance overlay — only the instance.example/ stub), the
  installed tree is a git repo with **zero remotes**, full code,
  and seeds a Semaphore-cloneable canonical — all git-free. (Only the docker
  `deploy-stack` bring-up is unexercised here — it's the existing verified flow on a
  clean target.) Replaces the `git clone` step in [SETUP.md](SETUP.md) §2.
- *Optional future hardening:* baking code **physically into the runner image** (so
  updates are an image pull, not even a local bundle). The bundle is now **public-clean** —
  it ships no `instance/` overlay (only `instance.example/`), so genericizing it for public
  distribution is **done**. This is dissemination polish, not a blocker — the instance is
  already fully git-optional.

### Phase D — Security + persona (G7, G8)  ← *done*
- **Trust boundary re-anchored** on where the state lives, not a remote: SOPS everywhere
  state flows + filesystem perms on the canonical/captures + leak-proof bundles/snapshots
  ([SECURITY.md](../SECURITY.md) C7 git-access, C8 local-state-at-rest).
- **`source_of_truth` is a real knob**, not a flag: [instance/instance.yml](../instance/instance.yml)
  declares the posture (`source_of_truth: local`) **and** the offsite `backup_remotes` that
  `scripts/backup.sh` pushes to (pluggable, multi-target). It's state (in the manifest).
- **Two personas, made explicit** (below + [SETUP.md](SETUP.md) §2).

## Two personas

| | **Operator** (runs an instance) | **Developer** (works on kontroll) |
|---|---|---|
| Gets the code | install bundle (`install.sh`) — **no git/GitHub** | clones the repo (read-only deploy key) |
| Source of truth | the local instance (`/srv/kontroll.git`) | the instance, synced from upstream via `update.sh` (merge) |
| Onboard a device | the GUI / `onboard` → local canonical | same |
| Update code | install a newer bundle | `update.sh` (merge upstream, preserve state) |
| Offsite backup | opt-in `backup.sh` → any remote (own scoped key) | same |
| Disaster recovery | `state-snapshot.sh` / `state-restore.sh` (no git) | same, or the git history |
| Needs a remote git? | **never** | only to pull upstream code (optional) |

The product (code) ships; the instance (state) is yours — git is an internal convenience
plus an optional backup, never a requirement.

## Trade-offs (eyes open)

- **Lost GitOps niceties** (single canonical record, free offsite history, PR review of
  infra) — mitigated by the *optional* remote: opt back in per-instance, never forced.
- **DR gets a new owner** — "it's in GitHub" is no longer the recovery story; G6's state
  backup must be designed up front, or a wiped VM is unrecoverable. This is the one place
  the change makes things *harder*.
- **Rollback/audit history is kept** — it lives in the local repo, just not a remote.

## See also
- [docs/SETUP.md](SETUP.md) — first-run + the work loop (rewritten in Phase D)
- [scripts/README.md](../scripts/README.md) — `onboard`'s local-vs-offsite push
- [docs/qa-and-release-pipeline.md](qa-and-release-pipeline.md) — the release bundle (G5)
- [SECURITY.md](../SECURITY.md) — the trust boundary re-anchor (G7)
