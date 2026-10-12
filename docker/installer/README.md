# docker/installer — the compose-native installer (Phase 1)

The install **glue** as an ephemeral container, so a fresh control node needs only **Docker + git** on the host —
no host ansible-core / device collections / sops / age (the version skew of which was the entire PR #87
fresh-Debian-12 bug class). The stack itself was already pure compose; this containerises the *installer*, not the
stack. Design-of-record: [docs/reviews/2026-06-29-compose-native-install/99-synthesis.md](../../docs/reviews/2026-06-29-compose-native-install/99-synthesis.md).

## What's here

| File | Role |
|---|---|
| `Dockerfile` | `debian:12-slim` + pinned ansible-core + the **control-plane** collections (`modules/_core.yml`, generated in the build by `gen-requirements.py --core`, installed via the unchanged never-brick wrapper) + sops/age + the docker CLI. Bakes **no** secret, age key, or `instance/`. |
| `entrypoint.sh` | Thin verb dispatcher — `init` · `check` · `fresh-init` · `configure-semaphore` · `shell` · `*` (ansible-playbook passthrough). No provisioning logic; every verb shells an existing playbook/script. |
| `../services/installer.yaml` | The ephemeral, portless, `network_mode: host` compose service (a standalone fragment — see *Why standalone*). |
| `../../scripts/kontroll-installer.sh` | The launcher: injects the operator identity (MF-1) + ensures the host bind targets exist, then runs the service. |

## The flow (host floor = Docker + git)

```
sudo bash scripts/install-prereqs.sh                                  # Docker + compose + git
sudo groupadd -g 1001 kontroll && sudo usermod -aG kontroll "$USER"   # the C10 canonical group (one-time)
scripts/kontroll-installer.sh fresh-init --mgmt-ip <ip> --domain <domain>   # scaffold instance/ + mint the control age key
$EDITOR instance/fleet.yml instance/inventory/hosts.yml instance/instance.yml
scripts/kontroll-installer.sh check          # deploy-stack --check --diff (the mandated dry-run)
scripts/kontroll-installer.sh                # bootstrap → deploy-stack (the stack comes up)
scripts/kontroll-installer.sh configure-semaphore
# then wire remaining secrets in the GUI Keys/Secrets dialogs (https://<ip>:8443)
```

The image is built locally on every `kontroll-installer.sh` run (`docker compose run --build` — near-noop when
nothing changed, but a bundle update that changes the Dockerfile is always reflected). A fleet change does **not**
require an installer rebuild — the device collections bake into the Semaphore runner image at deploy time; the
installer carries only the fleet-independent control-plane base (sops/general/docker).

> **Published path (C1):** the installer image is also published to private ghcr and pinned by `@sha256:` digest in
> [`../images.lock.yml`](../images.lock.yml). A fresh node can install from a **launch kit** (`scripts/make-launch-kit.sh`)
> with no clone and no build: [`scripts/kontroll`](../../scripts/kontroll.sh) (the published sibling of
> `kontroll-installer.sh`) reads `${KONTROLL_INSTALLER_IMAGE}` from the kit's `images.env`, `docker compose … pull init`
> + `run --no-build` against the digest, and forces `use_published_images=true` so the runner/vector pull by digest too.
> Same verbs, same playbooks — only build→pull differs. See [docker/README.md](../README.md) § Publishing images.

> **`check` on a truly fresh node:** run `init` before `check`. `--check` skips local-canonical's git-init
> *command*, so on a node with no `/srv/kontroll.git` yet the dry-run can't preview the very first install (a
> downstream `git ls-remote` of the not-yet-created canonical fails). After the first `init`, `check` is the
> `--check --diff` dry-run for every subsequent change. The onboard-gui age key is a host gesture the operator
> provisions once (the G9 guard fails the deploy closed until it exists — `cp` the control key to
> `${KONTROLL_STORAGE_ROOT}/onboard-gui/age.key`, owned `1001:1001 0600`).

## Security model (the floor that must hold — synthesis §5)

- **No secret in an image layer or a named volume.** The age key, the canonical, and the rendered `.env` are
  **host bind** files. A named volume is docker-socket-readable (wider than a 0600 host file), so it is forbidden;
  a repo-root `.dockerignore` keeps `instance/`, keys, and `.env` out of the build context.
- **Ephemeral, CLI-only, portless.** `run --rm`, no `restart:`, no `ports:`. The `:rw` docker socket makes the
  container host-root-equivalent **only for the run's duration** — so it must never become a standing surface.
- **`SOPS_AGE_KEY_FILE`, never the literal `SOPS_AGE_KEY`** (no private key value in `docker inspect`/the process env).
- **No keygen at build** (MF-3). The control age key is a host gesture — `fresh-init`/bootstrap mint it through a
  runtime `:rw` bind, never an image layer (which every puller would share).
- The age + ssh binds are `:rw` because this container is the privileged key-**minter**; the long-running network
  services keep their key binds `:ro`. `:rw` here does not widen blast radius — the socket already dominates.

These are pinned in CI by [tests/unit/test_installer_phase1.py](../../tests/unit/test_installer_phase1.py).

## Why a standalone fragment (not in `docker/compose.yaml`'s include)

A pre-`.env` run (`fresh-init`/`check` run before deploy-stack renders `docker/.env`) would otherwise trip the
stack services' `${…:?set in .env}` required-var validation, which compose evaluates for the **whole** project even
on a single-service `run`. Keeping the installer in its own fragment lets the first-boot verbs run with no `.env`
present. Phase B integrates the init services into the main compose with `depends_on`.

## Host / container split (MF-6)

The installer runs the playbooks unchanged **except** their host-only tasks, which skip when
`KONTROLL_IN_CONTAINER=1`: deploy-stack's Docker install (the host already has Docker — the floor) + the
qemu-guest-agent systemd unit (no systemd in a container) + bootstrap's OS-package/sops/gitleaks provisioning
(baked into the image) + local-canonical's named gid-1001 group (a host gesture). The host residual lives in
[scripts/host-systemd.sh](../../scripts/host-systemd.sh). Operator identity (MF-1) comes from
`KONTROLL_OPERATOR_{USER,UID,GID,HOME}`; on a host-direct run those are absent and the playbooks fall back to the
gathered facts (the same value), so nothing changes off-container.

## See also

- [docs/install-from-scratch.md](../../docs/install-from-scratch.md) — the full from-scratch walkthrough
- [docs/SETUP.md](../../docs/SETUP.md) — first-time + continual config
- [SECURITY.md](../../SECURITY.md) — the installer trust boundary
- [docker/README.md](../README.md) — the composed stack
