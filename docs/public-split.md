# The public split — how the public `kontroll` repository and a private instance repository relate

> Genericization Phase 5 (plan of record since 2026-06-15; executed 2026-10-08). Design-of-record for the
> identifier sweep and the cut: the private review dossier `docs/reviews/2026-10-08-public-split-pii-sweep/`.

## What is public, what is not

| Tree | Public repository (`github.com/netcanon/kontroll`) | Private instance repository |
|---|---|---|
| The tool — code, playbooks, registries, generators, tests, docs | **yes** (this is the source of truth for the tool) | mirrored |
| `instance.example/` — the placeholder overlay (TEST-NET addresses, one fake age recipient, canary tokens) | yes | yes |
| `instance/` — YOUR overlay: inventory, `.sops.yaml` recipients, encrypted secrets, fleet selection, Homepage tiles, app-store units, pinned public certificates, `leak-tokens.txt` | **never** — the root `.gitignore` ignores everything under it (`/instance/*`); the PII guard checks that nothing under it is tracked | tracked — `instance/.gitignore` (scaffolded by `kontroll-init --fresh` from `instance.example/.gitignore`) re-includes the overlay's known entries; the root `.gitignore` is identical in both repositories |
| `docs/reviews/` dossiers written before the split (they quote the maintainer's real topology) | never | tracked |
| `local/` | never (git-ignored everywhere) | never |
| Git history | **fresh** — the public repository starts at the cut commit | the full history |

The public repository has a **fresh history by design**: the private history carries the real
inventory, the real `.sops.yaml` recipients, SOPS ciphertext and lab addresses in hundreds of
commits and commit messages, and a scrub of free text across 600+ commits cannot be verified.
The private repository keeps that history as the archive.

## The cut (one-shot)

```bash
# on the private main, after the public-split prep landed
git archive --format=tar main | tar -x -C /tmp/public-tree
rm -rf /tmp/public-tree/instance /tmp/public-tree/docs/reviews/2026-*   # the strip set (= scripts/make-bundle.sh)
cd /tmp/public-tree && git init -b main && git add -A && git commit -s   # author: the project's public identity
# (the overlay rule lives in the tree's own .gitignore — `/instance/*` — so the cut edits nothing)
```

The acceptance gate for the cut is `python3 tests/_leak_guard.py --tree` returning **zero findings on
the public tree with the private token list active** (`KONTROLL_LEAK_TOKENS_FILE=…/instance/leak-tokens.txt`),
plus a green `tests/validate.sh --strict` and the hermetic pytest suite on the public tree — the tree must
pass its own gates with no `instance/` present (`kontroll.paths.resolve` reads the shipped example on an
unconfigured checkout).

## The standing guard (why it cannot recur)

`tests/_leak_guard.py` runs in three places with one implementation: `tests/validate.sh` (step `pii-guard`),
`.github/workflows/pii-guard.yml` (the required check *No leaked personal identifiers*) and the pytest twin
`tests/unit/test_leak_guard.py`.

- **Structural layer** (public, always on): any RFC-1918 address in any spelling (dotted, dashed, underscored),
  a non-documentation MAC or global IPv6, a real-length age recipient / AGE secret key, the maintainer's
  personal email, an operator-machine user-profile path. TEST-NET, RFC-7042 MACs and `2001:db8::/32` are the
  sanctioned examples and never match.
- **Instance-token layer** (private): the names that identify one deployment — hostnames, domain, user names,
  subnets, VLAN ids — one regex per line in `instance/leak-tokens.txt`. It never ships; the public repository
  receives the same list as the repository secret `KONTROLL_LEAK_TOKENS` (fork PRs have no secrets and run on
  the shipped canaries). A match is reported as `token#N sha256:xxxxxxxx`, never the name.
- A line that must carry a private address says so: `pii-guard: allow <reason>`.
- **The overlay invariant** (`--tree`, all three seats): a tree is PUBLIC — nothing under `instance/` is tracked
  and the root `.gitignore` ignores the directory's entries — or INSTANCE — the overlay is tracked and every
  tracked path is re-included by `instance/.gitignore`. A `git add -f` on a public tree, a root rule that went
  missing, or an instance repository without its re-include file fails the gate with the paths. `docs/reviews/`
  is scanned on a public tree and skipped only on an instance tree (its dossiers quote the real deployment).

## CI in the two repositories

The workflows are identical files. Every job's `runs-on` is
`${{ fromJSON(vars.KONTROLL_RUNS_ON || '"ubuntu-latest"') }}`:

| | public | private instance repo |
|---|---|---|
| `KONTROLL_RUNS_ON` variable | unset → GitHub-hosted `ubuntu-latest` (free for public repos; a public repo must never use a self-hosted runner — a fork PR would run on it) | `["self-hosted","Linux","X64"]` — the org runners |
| `KONTROLL_LEAK_TOKENS` secret | the private token list | not needed (`instance/leak-tokens.txt` is tracked) |
| `validate --strict` | every tool installed in the job (sha256-pinned sops, gitleaks, promtool, vector) | same |
| Rulesets | `main`: require a pull request + every CI check green, no force-push, no deletion; `v*` tags immutable | not available on the plan (checks are advisory) |
| Secret scanning + push protection, CodeQL default setup, Dependabot alerts | on | not available on the plan |
| `publish-images.yml` | publishes `ghcr.io/<owner>/kontroll-*` (the owner read from the repository, nothing hard-coded) on a `v*` tag or by `workflow_dispatch`. `docker/images.lock.yml` names the public owner's packages (`v0.1.2`, by digest); they are public since 2026-10-11, so every pull is anonymous and no clone needs a registry credential (the package setting is manual and one-way, not part of the repository flip — SECURITY.md R-IMG-1, retired); the default local build is unaffected | the same file publishes the instance's own packages, if it ever needs them |
| `KONTROLL_CODE_SCANNING` variable | `true` after the flip → the zizmor job also uploads SARIF to code scanning (free on a public repo) | unset (code scanning is a GHAS feature on a private plan; the upload would 403) |

## Day-to-day — the weekly sync

- **Tool changes land on the public repository first**: feature branch → pull request → the required checks →
  squash-merge. That is where the enforcement is. The private instance repository *receives* them by sync; it
  never carries a tool fix of its own first (a private-first fix comes back from the public squash with review
  edits and conflicts at the next sync — if a production emergency forces one, resolve that file to the public
  side at the next sync). A fix cherry-picked from a pre-split private branch is scrubbed before it goes up.
- **The private repository tracks the public one through a merge, never a rebase or a squash.** `upstream`
  is the public repo with its push URL set to `DISABLED`; the first sync was a one-time
  `--allow-unrelated-histories` graft, every later one is a plain merge. The private repository allows **merge
  commits only** (squash and rebase merging are switched off in its settings): a squash would drop the graft
  and the next sync would ask for unrelated histories again. If git ever asks for `--allow-unrelated-histories`
  on a sync, stop — the graft was lost.

```bash
# the weekly sync, in the private instance repository
git fetch upstream --no-tags                      # remote.upstream.tagOpt is --no-tags: public tags never land here
git switch -c sync/upstream-$(date -u +%F) origin/main
git merge --no-ff --no-edit upstream/main          # conflicts: see the rules below
git diff --name-status upstream/main HEAD -- . ':!instance' ':!docs/reviews'   # must print NOTHING
bash tests/validate.sh --strict && python3 -m pytest -m "not e2e and not slow" -q
git push -u origin HEAD && gh pr create --fill && gh pr merge --merge     # a MERGE commit, never squash/rebase
git fetch origin && git merge-base --is-ancestor upstream/main origin/main && echo "graft intact"
```

Conflict rules:
- `.gitignore` is identical in both repositories (the overlay rule `/instance/*` lives in the tree; the instance
  repository tracks `instance/.gitignore`, which re-includes its overlay). If a sync ever shows a diff there, the
  instance side drifted — take the public side. The PII guard fails an instance tree whose `instance/.gitignore`
  went missing (tracked AND ignored) before the next deploy can silently drop new overlay files.
- `CHANGELOG.md`: the private side never edits it (private-only notes go in `instance/`); public entries all
  insert under `## [Unreleased]`, newest first, so a sync never conflicts there.
- `docker/code-manifest.lock.yml`: regenerate (`python3 scripts/gen-code-manifest.py`), never hand-merge.

After the sync merges: on the control node `scripts/update.sh` (origin `main`) pulls it into the local canonical,
then `deploy-stack` (the api container runs a deploy-managed copy, so a restart alone runs stale code).

Also weekly:
- **Leak-token parity**: the public secret `KONTROLL_LEAK_TOKENS` must equal `instance/leak-tokens.txt`. The
  PII-guard job prints the pattern count it loaded; compare it with `grep -cvE '^\s*(#|$)' instance/leak-tokens.txt`
  and re-set the secret whenever the file changes (`gh secret set KONTROLL_LEAK_TOKENS --repo <public> < instance/leak-tokens.txt`).
- **Dependabot opens the same bumps on both repositories** (the file is shared): merge on the public side, close
  the private twin, receive it by sync.
- **Tags**: the private repository's `v0.1.x` tags predate the split; the public line starts above them. Never
  push a public tag to the private remote (its `publish-images.yml` would re-publish under the private org).
- **The image lock**: the private box deploys the public owner's packages, which are public — the pull is
  anonymous and the box needs no `ghcr.io` credential (SECURITY.md R-IMG-1, retired 2026-10-11). A `docker login
  ghcr.io` left over from the private period is harmless and unused; `docker logout ghcr.io` removes it.
- The PII guard runs on GitHub-hosted runners even on the private repository (a public-repo guard must never use
  the self-hosted ones), so it spends the private plan's hosted minutes; if that budget is exhausted the guard
  stops running on private PRs — loudly.
- Code scanning: the private org's code-security configuration does not reach the public repository (a different
  owner); CodeQL default setup there is the per-repository step in the checklist.
- The `homelab` tine is caught up with ONE merge from `main` after a sync, not a rebase of its whole history.

## Publishing the images (the re-pin, once)

`docker/images.lock.yml` is re-pinned from the pre-split private org's images to the public owner's in this order
— a re-pin that lands before the packages are pullable breaks every published-image deploy that syncs it:

1. `publish-images.yml` is owner-agnostic and its third-party actions SHA-pinned (public PR #9); `gen-image-digests.py
   --refresh` fails closed.
2. Pre-flight: `gh api 'users/<owner>/packages?package_type=container'` shows no stale `kontroll-*` package that this
   repository is not granted to (the `GITHUB_TOKEN` push would be refused).
3. `gh workflow run publish-images.yml --ref main -f tag=sha-<short main>` (the workflow checks the tag names the
   commit it builds); wait for the three images.
4. Make the three packages public — part of the operator-approved flip, because it cannot be undone (GitHub has no
   API for package visibility; each package's settings page).
5. The re-pin PR: `python3 scripts/gen-image-digests.py --refresh sha-<short> --owner <owner>` (one command: refs,
   tag, digests; nothing is written if a digest does not resolve); check each image's
   `org.opencontainers.image.revision` label names that commit (`docker buildx imagetools inspect <ref>@<digest>
   --format '{{json .}}'`); from a **logged-out** shell, `docker buildx imagetools inspect <ref>@<digest>` succeeds
   for all three; then merge.
6. Instance side: confirm the control node's registry credential (above), then a dry run before the first deploy
   that pulls the new digests. Later releases re-pin to a `v*` tag, which the public ruleset makes immutable; the
   `sha-` tag is the one-off bridge.

## Operator checklist for the first publish

1. Create the public repository **private**, push the cut, let CI run green (GitHub-hosted).
2. Set the repository secret `KONTROLL_LEAK_TOKENS` from `instance/leak-tokens.txt`.
3. Flip the visibility to public; immediately apply the rulesets (`main` required checks with
   `strict_required_status_checks_policy: true`, immutable `v*`), enable secret scanning + push protection, CodeQL
   default setup, Dependabot alerts + security updates, and set the repository variable
   `KONTROLL_CODE_SCANNING=true` (the zizmor SARIF seat; dispatch `zizmor.yml` once to see the first upload).
4. Re-run the full identifier sweep (`tests/_leak_guard.py --tree` with the private list) on the pushed tree —
   the same command, the same zero.
5. Publish the images and make the three packages public, then merge the re-pin (the order above). Done
   2026-10-11: `v0.1.2` published and pinned, the three packages public, anonymous pulls verified against the
   lock's digests (by tag and by digest).
