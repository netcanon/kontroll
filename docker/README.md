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
4. Bind the config the service reads **read-only** (`../<dir>:/etc/<x>:ro`), from the tree or as a file the
   deploy renders under the storage root: the deploy restarts a service whose read-only config binds changed since
   it loaded them (`scripts/stale-config.py`), so a service that reads its config only at start still gets each
   new build's. A read-write bind, or a directory under the storage root, is the service's own state and is not
   watched. A bind the service reloads by itself goes in its `kontroll.config.live` label (the targets,
   space-separated), so a change to it restarts nothing.

## Errors / behaviour
| Condition | Behaviour |
|---|---|
| Missing required `${VAR}` | `compose config` fails (fail-closed) — validate catches it |
| Missing `docker/.env` | optional `env_file` (`required: false`) — service starts without live widgets |
| Read-only config changed, service definition not | the deploy restarts it after `up`, then records it (step 4) |
| The same, under `--check` | named (`would restart …`); a regenerated config is not previewed |

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
  [`images.lock.yml`](images.lock.yml); commit it, with [STATUS.md](../STATUS.md)'s `**Images:**` line set to the
  new tag in the same commit (`tests/check-status.py` fails a lock and a page that disagree). No login — the packages are public (a `docker login ghcr.io`
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

### Refreshing a pin

Every input an image build or the stack pulls is pinned, and
[`tests/unit/test_image_pins.py`](../tests/unit/test_image_pins.py) fails when a pin is **missing** (never because
one is old):

| Input | Pinned as | Where |
|---|---|---|
| a Dockerfile's base | `FROM <image>:<tag>@sha256:<digest>` — the digest is pulled, the tag is its label | each `Dockerfile` (Vector's also in `services/vector.yaml`, in lock-step) |
| the Python packages an image adds | one `name==version` per package, installed `--no-deps`, then `pip check` | `semaphore-runner/requirements.txt`, `installer/requirements.txt` |
| the runner's apk packages | `~=<major.minor>` (the patch revision still moves: an Alpine branch keeps only its newest) | `semaphore-runner/Dockerfile` |
| a downloaded file (a binary, an apt key) | its SHA-256, written in the tree: a `curl`/`wget` to a named file, then `echo "$<ARG>  <file>" \| sha256sum -c -` in the same `RUN` (an `ARG` or `ENV` holding the 64 hex), or `ADD --checksum=sha256:` | `installer/Dockerfile` (sops, Docker's apt key) |
| a compose image | an exact tag (at least major.minor), or a digest under a tag | `services/*.yaml`, `../telemetry/*.yml` |

A check that is switched off counts as missing. A `pip install`, `pip check`, `apk add`, download or `sha256sum -c`
fails the build only if every operator after it to the end of the `RUN` is `&&` (a pipe after a later `&&` too): a
`|| true` or `; rm …` anywhere later, a pipe right after it, an `if`, `elif`, `while` or `until` condition around it
or a `!` before it switches it off. Under `set -e` (or `set -o errexit`), written as a whole command at the top of the
`RUN`, a `;` right after the check is enough when the check is the whole of its `&&` list — not after a later
`&& …` (errexit spares every command of a list but the last), not after a guard (`[ "$TARGETARCH" = amd64 ] && check;`
skips the check when the guard fails), and not inside a brace group, a subshell, an `if` body or a loop (a `|| true`
after the compound switches errexit off inside it). A `set +e` anywhere, a `set` of a variable or a `shopt` naming
errexit switches errexit off. A check inside a `$(…)`, backquotes or a quoted string (`sh -c '…'`, `eval '…'`) is
refused, as is a `trap`, an `exec`, a `return` or an `exit` other than `exit 1` to `exit 255` in the same `RUN`: each
can end it with a status that is not the check's. A download names one URL and one output. A checksum fetched beside
the file is no pin either — whoever swaps the file swaps it too — so the digest must be one the tree holds: an `ARG`
or `ENV` whose every value is the 64 hex, on one `echo "$<ARG>  <file>" | sha256sum -c -` line, and no build passes
that `ARG` in. The rules are in [`tests/unit/_build_read.py`](../tests/unit/_build_read.py), and the pip-audit step is
held to them too (and must name its shell unless its runner is a named Linux or macOS one). The test also refuses
what it cannot read (a `SHELL` other than Docker's default, an exec-form `RUN`, a heredoc, a comment inside a `RUN` —
Docker joins the lines first, so the shell drops the rest — an `# escape=` directive, a Dockerfile outside this
directory) and a `# syntax=` frontend not pinned by digest. What it still cannot see is listed in the test's
docstring. The installer's and Vector's **apt** packages still float inside their pinned base; the
published image's digest in [`images.lock.yml`](images.lock.yml) is the full pin ([SECURITY.md](../SECURITY.md) §
Accepted risks, R-IMG-2).

One procedure, the same for a Dependabot PR and a hand bump. It is an authoring-time act with network; nothing in
it runs on a deploy path.

1. **Change the tag** in the `FROM` line or the compose `image:`. Dependabot opens the PR for a Dockerfile base
   and for a pip pin. A compose `image:` has no bot (Dependabot's compose fetcher reads no `<service>.yaml` name),
   so every compose tag, Postgres's in `services/semaphore.yaml` among them, is bumped here by hand. The exporter
   images are generated: bump them in `telemetry/*.yml` and run `scripts/gen-exporters.py`.
2. **Resolve the digest** for that exact tag, and write it after the tag: `docker buildx imagetools inspect
   <image>:<tag>`, taking the top-level `Digest:` (the manifest list, so one pin serves every platform the publish
   matrix builds). Bumping only a pip, apk or checksum pin skips this step.
3. **The control image: re-derive what the base decides.** Build it on the new base, then:
   - regenerate `semaphore-runner/requirements.txt` as the difference between `pip freeze` in the bare base's
     ansible venv and `pip freeze` after `pip install paramiko flask fastapi uvicorn PyYAML` there (sorted, one
     `name==version` per line; the header stays). A package the base already ships stays out of the file — the
     Dockerfile's import line holds it — unless the base stops shipping it;
   - re-read the apk constraints with `apk policy <package>` if the Alpine branch moved.

   **The installer:** its `requirements.txt` is `pip freeze` in a build with `"ansible-core==<series>.*"`,
   `ansible-lint` and `docker` installed (less Debian's own pip, setuptools and wheel). `<series>` stays inside the
   range `scripts/install-prereqs.sh` puts on the host, which `tests/unit/test_image_pins.py` holds: unconstrained,
   the resolve lands on the next series. A new sops version changes `SOPS_VERSION` and
   both `SOPS_SHA256_*` from the release's `checksums.txt` together.
4. **Prove it.** Build every image from the change (the control image after `python3
   scripts/gen-requirements.py --all-modules`, as the publish workflow does) — `pip check`, the import line, the
   checksums and the apk constraints run in the build. In the built control image, `ansible --version`, `sops
   --version`, `age --version`, `snmpbulkwalk -V` and `ssh -V`; copy `/opt/kontroll` out and run
   `scripts/gen-code-manifest.py --verify` on it. In the installer, `ansible --version`, `ansible-galaxy collection
   list` and `docker compose version`. A Semaphore base must be at least the version every live box runs: Semaphore
   migrates its database forward on start, so an older pin is a downgrade over a migrated schema. No pull-request
   job builds an image, so a PR that moves an image is merged only after this step.
5. **Publish and re-pin** (above): tag, publish, `gen-image-digests.py --refresh <tag>`.

### C1 — the installer image + bundle-as-compose

C1 publishes the **installer** image too, so a fresh node installs from published images with **no git clone and no
`docker build`**:

- The installer joins the publish matrix; `docker/services/installer.yaml`'s ref becomes
  `${KONTROLL_INSTALLER_IMAGE:-kontroll/installer:latest}`. The same fragment serves both paths —
  [`scripts/kontroll-installer.sh`](../scripts/kontroll-installer.sh) does `run --build` (local build);
  [`scripts/kontroll.sh`](../scripts/kontroll.sh) pulls each image by its published digest (`docker pull`) and
  then does a plain `run` — the image is present, so nothing is built.
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
The pins are held by `tests/unit/test_image_pins.py`, and their Dependabot and pip-audit coverage by
`tests/unit/test_ci_workflows.py` — both pure text, no Docker.

## See also
- [installer/README.md](installer/README.md) — the compose-native installer (Phase 1)
- [../PLAN.md](../PLAN.md) §3 (component selection), §8 (dashboards)
- [../dashboards/README.md](../dashboards/README.md), [../prometheus/README.md](../prometheus/README.md)
