# docker — the composed stack

The control plane's services, each in its own compose fragment. **Semaphore, Homepage,
and the API are deployed** (Phases 3–4); the **Phase-5 metrics stack — Prometheus +
Grafana + pve-exporter — is deployed** too.

Deploy is codified, not manual: `ansible/playbooks/deploy-stack.yml` installs Docker,
renders `docker/.env` from the SOPS `semaphore`/`dashboards`/`proxmox` domains,
regenerates the Prometheus scrape targets from the modules+inventory
(`scripts/gen-observability.py`), and brings up the data-driven `stack_services` list.
Re-run it to add services as later phases enable them.

> **Compose-native install (Phase 1):** the install glue itself now runs in an ephemeral container —
> [`installer/`](installer/README.md) (built from `installer/Dockerfile`, run via `services/installer.yaml` +
> `scripts/kontroll-installer.sh`). It is a STANDALONE fragment (not in `compose.yaml`'s include) so first-boot
> verbs run before `docker/.env` exists. Host floor: Docker + git.

## Architecture
- `compose.yaml` — composition ONLY (no service definitions). Brings the others
  in via `include:`. Adding a service = add a file + one `include:` line.
- `services/<name>.yaml` — one self-contained service: Semaphore (Ansible UI),
  Homepage (portal), the API, Prometheus, Grafana, pve-exporter (Proxmox metrics).
- `installer/` — the ephemeral, portless compose-native installer image + entrypoint (the install glue; NOT a
  stack service — see [installer/README.md](installer/README.md)).
- `.env.example` — template for `docker/.env` (gitignored); real values come from
  the SOPS `dashboards`/`semaphore` domains at deploy.
- The shared `kontroll` network is **external** (declared so in every fragment so
  they can run standalone); `deploy-stack.yml` creates it once.

## Adding a service (the "add an X" test)
1. Write `services/<name>.yaml` (copy an existing one; attach to the `kontroll`
   network; pull secrets via `env_file`/`${VAR}`).
2. Add one `- services/<name>.yaml` line to `compose.yaml`.
3. `docker compose -f docker/compose.yaml config` (in `tests/validate`) must pass.

## Errors / behaviour
| Condition | Behaviour |
|---|---|
| Missing required `${VAR}` | `compose config` fails (fail-closed) — validate catches it |
| Missing `docker/.env` | optional `env_file` (`required: false`) — service starts without live widgets |

## Publishing images (C0/C1 — ghcr, pull by digest)

The Semaphore runner, Vector, and installer images can be **published once** to
`ghcr.io/<owner>/kontroll-{control,vector,installer}` — the publishing repository's owner, never hard-coded — and
pulled BY DIGEST instead of built on every control node (report 22). **Opt-in** — the local `docker build` path is
the default and unchanged. A **new package starts private** whatever the repository's visibility (the first publish
from this public repository, 2026-10-09, produced private packages); the owner makes it public once, by hand, in the
package's settings (no API exists for it, and a public container package cannot be made private again). The public
tool's three packages **are public since 2026-10-11** — every pull is anonymous.

- **Publish** (a release tag, or `workflow_dispatch`) → [`.github/workflows/publish-images.yml`](../.github/workflows/publish-images.yml)
  builds + pushes with the built-in `GITHUB_TOKEN`. The runner bakes the **all-modules superset**
  (`gen-requirements.py --all-modules`) — a published image can't know a node's fleet.
- **Pin the digests:** `python3 scripts/gen-image-digests.py --refresh <tag>` writes `@sha256:` into
  [`images.lock.yml`](images.lock.yml); commit it. No login — the packages are public (a `docker login ghcr.io`
  with `read:packages` is needed only for a private package). Docker verifies the digest on every pull (the
  image-world L2b — [SECURITY.md](../SECURITY.md) § Accepted risks, the retired R-IMG-1 row).
- **Deploy with published images:** `… deploy-stack.yml -e use_published_images=true` — the two image-build tasks
  skip and `docker/.env` pins `KONTROLL_{CONTROL,VECTOR}_IMAGE` from the lock (fail-closed if no digest is recorded).
  The runner refs are `${KONTROLL_CONTROL_IMAGE:-kontroll/semaphore:latest}` (the `:-` default keeps local builds working).

`scripts/gen-image-digests.py --env`/`--check` are OFFLINE; only `--refresh` touches the registry (dev/CI authoring).
Cosign signing is the deferred rung.

- **Baked-code manifest (M6 — "L2b for code"):** the control image bakes the importable package `scripts/kontroll/`
  at `/opt/kontroll` (Phase B). Where [`images.lock.yml`](images.lock.yml) pins the image *bytes* by digest,
  [`code-manifest.lock.yml`](code-manifest.lock.yml) records a per-file sha256 floor for that *code*. `python3
  scripts/gen-code-manifest.py` regenerates it after any `scripts/kontroll/` edit (validate + the publish `gate` run
  `--check`, so a stale/forgotten regen fails loudly and a tag can't publish drifted baked code); `--verify <tree>`
  re-checks a materialized baked tree (`/opt/kontroll`) fail-closed for an audit. OFFLINE (pure file hashing).

### C1 — the installer image + bundle-as-compose

C1 publishes the **installer** image too, so a fresh node installs from published images with **no git clone and no
`docker build`**:

- The installer joins the publish matrix; `docker/services/installer.yaml`'s ref becomes
  `${KONTROLL_INSTALLER_IMAGE:-kontroll/installer:latest}`. The same fragment serves both paths —
  [`scripts/kontroll-installer.sh`](../scripts/kontroll-installer.sh) does `run --build` (local build);
  [`scripts/kontroll.sh`](../scripts/kontroll.sh) does `pull init` + `run --no-build` (published digest).
- **Build a launch kit:** after a publish + `gen-image-digests.py --refresh <tag>` + commit,
  [`scripts/make-launch-kit.sh`](../scripts/make-launch-kit.sh) assembles `kontroll-launch-<ver>.tar.gz` — the
  instance-stripped tree + `images.env` (the digest pins) + the `kontroll` launcher (no `instance/`, no secrets).
- **Install from the kit** (host floor = Docker + git): `tar xzf … && cd kontroll-launch-<ver> && ./kontroll
  fresh-init …` → edit `instance/` → `./kontroll check` → `./kontroll init`. The launcher pulls every image by digest.

The Index "Supply chain" section carries an **images sub-panel** (`GET /api/image-provenance`) that reports each
kontroll image's honest class — `digest-pinned` (amber: Docker verifies the `@sha256:` on pull, not a signature),
`local-build`, or `signed` (cosign, deferred).

**Air-gapped install:** [`scripts/make-bundle-airgap.sh`](../scripts/make-bundle-airgap.sh) pulls every kontroll image
by digest, `docker save`s them into one tar, and bundles the launch kit → `dist/kontroll-airgap-<ver>-<arch>.tar.gz`.
On the disconnected node: `docker load -i kontroll-images-…tar` then `./kontroll fresh-init …` — zero registry
reachability (the digest-pin survives save/load; the `kontroll` launcher skips the pull when the image is already
loaded). Remaining deferred: **full launch-kit file-set minimization is Phase-B-coupled** — the stack services run
code from the `/repo` bind / the cloned canonical (`api.main`, `gui/app.py`, the playbooks), so the kit must ship the
full tree until those run *baked* code (Phase B).

## Testing
Covered by `tests/validate`'s `compose-config` check (structure validated against
`.env.example`) + `gen-image-digests --check` (the lock is well-formed). Runs only where Docker is installed.

## See also
- [installer/README.md](installer/README.md) — the compose-native installer (Phase 1)
- [../PLAN.md](../PLAN.md) §3 (component selection), §8 (dashboards)
- [../dashboards/README.md](../dashboards/README.md), [../prometheus/README.md](../prometheus/README.md)
