# SETUP — first-time & continual control-node config

The canonical, reproducible procedure to stand up (or rebuild) the kontroll
control node. Written so a future operator **or an AI agent** can execute it cold.
It reflects the real, tested flow — the bulk is automated by
`ansible/playbooks/bootstrap.yml`, driven by your `instance/fleet.yml`.

> The modularity contract: you declare your hardware in **one file**
> (`instance/fleet.yml`) and run **one playbook**. Disabled modules cost nothing;
> adding hardware = add a `modules/<key>/` dir. See [modules/README.md](../modules/README.md).

---

## 0. Provision the control VM

A Debian 12/13 VM, 2 vCPU / 4 GB / 32 GB, on your management VLAN, with a static
IP. For the Proxmox-specific `qm` recipe (cloud-init image, disk, NIC) see
[bootstrap-control-vm.md](bootstrap-control-vm.md). Any Debian host works — **or any
mainstream Linux**: `install-prereqs.sh` auto-detects your package manager
(apt/dnf/zypper/pacman) for the host floor, and everything else runs in the
installer container, so the host distro is otherwise irrelevant.

## 1. Stage 0 — system prerequisites

> **Compose-native (Phase 1):** the host floor is now just **Docker + git** — the ansible/sops/age toolchain runs in
> an ephemeral installer container, and the whole flow is `scripts/kontroll-installer.sh <verb>` (see the quickstart
> in [docs/install-from-scratch.md](install-from-scratch.md) + [docker/installer/README.md](../docker/installer/README.md)).
> The steps below are the legacy host-ansible path, still valid after `install-prereqs.sh --with-host-ansible`.

On the compose-native path this is the quickstart's `sudo bash scripts/install-prereqs.sh` — Docker, compose and
git, the whole host floor — and you follow the quickstart from there. The commands in §4, §5 and §7 below belong
to the legacy host-ansible path, which needs Ansible, sops and age on the host as well:

```bash
./bootstrap.sh --with-host-ansible     # → scripts/install-prereqs.sh: the host floor PLUS ansible-core, sops, age
```

Idempotent (safe to re-run). `bootstrap.sh` is now a thin shim over
[scripts/install-prereqs.sh](../scripts/install-prereqs.sh) — run either.

## 2. Get the code onto the node

**Git-free (recommended for an operator):** install from a release bundle — **no GitHub
account or clone needed.** On a machine with the code, `bash scripts/make-bundle.sh`
produces `dist/kontroll-<version>.tar.gz`; copy it to the node (scp/USB/any transfer) and follow the quickstart at
the top of [install-from-scratch.md](install-from-scratch.md) from its first step ("a release bundle"): the bundle
becomes a local git tree with no remote, and `fresh-init` scaffolds your overlay.

`--trust-mode separated`, in that scaffold command, puts a human review gate on every staged proposal (the
recommended posture). What `solo` does, and what the scaffold does when the flag is left out, depends on the
edition you are installing — read [trust-mode.md](trust-mode.md) before choosing anything else.
`kontroll-init --fresh` — what `fresh-init` runs — scaffolds your private `instance/` overlay from the shipped
`instance.example/` skeleton (your keys only — it never inherits the bundle author's
recipients), mints your control age key, seeds `instance/.sops.yaml` with that key and the
break-glass recipient you choose (`--break-glass-recipient`, `--mint-break-glass` or
`--no-break-glass`; [break-glass.md](break-glass.md)), fills
`instance/instance.yml`'s `mgmt_ip`/`domain` **and the Homepage portal tiles** (a fresh
`:3000` shows your real control-plane services, not the shipped TEST-NET stubs), and mints
the `dashboards` and `semaphore` bootstrap secrets, encrypted to every recipient (the
passwords shown once — save them). The instance is the source of truth
([docs/local-source-of-truth.md](local-source-of-truth.md)); git is used only locally.
Continue at §3. The rest of this section is the **developer** path (a git-connected instance).

**Bundle-as-compose (C1, published images):** if the images are published to ghcr, build a **launch kit** instead —
`bash scripts/make-launch-kit.sh` produces `dist/kontroll-launch-<version>.tar.gz` (the same instance-stripped tree
**plus** digest pins + a `kontroll` launcher). On the node (no registry login — the packages are public) `tar xzf …`, `cd
kontroll-launch-<version>`, and use `./kontroll fresh-init … / check / init` — it pulls every image **by digest**, so
there is no `docker build` on the node at all. See [docker/README.md](../docker/README.md) § Publishing images.

**Developer path — clone via a read-only deploy key** (least privilege):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/kontroll_deploy -N "" -C "control-vm-deploy"
cat ~/.ssh/kontroll_deploy.pub
#   add it at: repo → Settings → Deploy keys (read-only)
#   or from a machine with gh:  gh repo deploy-key add <file> -R <owner>/kontroll
printf 'Host github.com\n  IdentityFile ~/.ssh/kontroll_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
chmod 600 ~/.ssh/config
ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts 2>/dev/null

git clone git@github.com:<owner>/kontroll.git
```

A clone of the tool has no `instance/`: scaffold yours as the quickstart does (`fresh-init`), then continue at §3.

## 3. Declare your fleet + your hosts

Edit your overlay (scaffolded by `fresh-init`, §2):
- **`instance/fleet.yml`** — enable the modules you actually run (catalog in
  `config/fleet.example.yml`). This is the file most setups touch. Each entry of `enabled_modules` is a
  device-class key — the name of a directory under `modules/`: lowercase letters, digits, `_` and `-`, starting
  with a letter or digit. A line that is not one (an unquoted `no`, `off` or `~`, which YAML reads as another
  type; a typo with a slash; a capital letter) stops the bootstrap and a deploy — and, if it is already on the
  canonical's `main` (a box that deployed it under an older build), every promote until it is corrected — each
  naming the entry by its place among the list's entries (comment and blank lines are not counted).
  [troubleshooting.md](troubleshooting.md) §6 says what each message means.
- **`instance/inventory/hosts.yml`** — your hosts, in functional groups
  (`edge_firewall`, `core_switch`, …).
- **`instance/instance.yml`** — confirm `mgmt_ip` / `domain` (set by `--fresh`) and
  pick a `frontend.tls_mode`.

## 4. Stage 1 — run the bootstrap

On the compose-native path the quickstart's bare `scripts/kontroll-installer.sh` runs this playbook and then the
deploy. On the legacy host path it is `ansible-playbook playbooks/bootstrap.yml`, run from `ansible/`; the rest
of that sequence is [install-from-scratch.md](install-from-scratch.md) §3–§6.

It is idempotent and does everything we used to do by hand:
- checks first that every `enabled_modules` entry of your fleet is a device-class key, and stops with the
  position of a line that is not one (`scripts/gen-requirements.py --fleet-check`),
- generates `requirements.generated.yml` from `_core` (which lists every collection the install playbooks
  import, whatever the fleet) + your enabled modules via the
  single-source `scripts/gen-requirements.py` (the same resolver deploy-stack + CI use) and
  installs **only those** collections,
- installs the toolchain (age, sops, qemu-guest-agent),
- generates the **age key** if absent and prints its public recipient,
- reports which `instance/secrets/<domain>.sops.yml` files your fleet needs.

## 5. Wire secrets (one-time)

**The fast path — `kontroll-init`** (the keygen split's CLI half — does steps 1 + the GUI/Grafana/API
bootstrap secrets a *running* GUI can't set for itself):

```bash
python3 scripts/kontroll-init.py     # existing overlay: ADD your control recipient (additive) + mint dashboards bootstrap secrets
```

(A brand-new node should instead have run `kontroll-init --fresh …` in §2, which SEEDS
`instance/.sops.yaml` with your key and the break-glass recipient you chose, never another
instance's, rather than appending to an existing overlay.)
It ensures the control age key, adds its public half to `instance/.sops.yaml` (additive +
parse-verified), and mints
the onboard-GUI password (`GUI_PASSWORD`), the Grafana admin password, and the C10 API token into the
`dashboards` SOPS domain — **showing the login passwords once** (save them). Then check the break-glass
recipient (step 2 below) and add the remaining service secrets (the GUI **"Secrets"** dialog after deploy, or `sops`).
The manual equivalents:

1. Put the age recipient the bootstrap printed into **`instance/.sops.yaml`** (replace the
   placeholder). *(kontroll-init does this.)*
2. **The break-glass recovery key.** A brand-new node chose it at `kontroll-init --fresh`
   (`--break-glass-recipient`, `--mint-break-glass` or `--no-break-glass`), and the first
   secrets were written to both recipients. If you declined it, or this overlay predates
   the choice, every domain has ONE recipient: add one now with
   [break-glass.md](break-glass.md) — it makes the key on a trusted machine, adds its
   public half under both recipient anchors and re-wraps the existing files — before
   creating any further secrets. Either way, prove the key opens every domain (that
   page's "Verify"): kontroll counts recipients and cannot know whether the private half
   was kept. Also keep a copy of the control-node key (`~/.config/sops/age/keys.txt`) in
   your password manager — a backup of that key, not a second recipient.
3. Create the per-domain secret files the bootstrap listed. Prefer the GUI **"Secrets"**
   dialog after deploy (the value is encrypted on the box, never in your shell history);
   the CLI fallback is:
   ```bash
   sops instance/secrets/network.sops.yml     # device creds / API tokens
   sops instance/secrets/proxmox.sops.yml      # PVE API token
   ```

## 6. Deploy the stack and open the GUI

On the compose-native path this is the end of the quickstart: the bare `scripts/kontroll-installer.sh` seeds the
local canonical and brings the composed stack up (it renders `docker/.env` from `instance/instance.yml` and SOPS),
and `scripts/kontroll-installer.sh configure-semaphore` then wires the Semaphore that is now running on `:3001`.
The legacy host form of the same steps — `local-canonical.yml`, `deploy-stack.yml`, `configure-semaphore.py` — is
[install-from-scratch.md](install-from-scratch.md) §3, §5 and §6: the two plays from `ansible/`, the script from
the tree root. The GUI and API surfaces described below come up once you have named those services
(install-from-scratch.md §7), not with the default deploy.

The stack binds the GUI, the API, Grafana, Prometheus, Loki, the syslog ingress (`:5514`) and, under `acme`,
Caddy (`:80`/`:443`) to your `mgmt_ip` only. The
exceptions are **Homepage (`:3000`, unauthenticated by design) and Semaphore (`:3001`, behind its own login)**,
which are published on all interfaces because they are meant to sit behind your reverse proxy — on a host with an
interface outside the management network, firewall those ports (SECURITY.md C3; `tests/check-mgmt-port-binding.py`
pins exactly this split). The GUI and the API serve HTTPS under the default `frontend.tls_mode: self_signed`.
Under `byo_proxy` both serve plain HTTP for your reverse proxy to front. Under `acme` both also serve plain HTTP:
the bundled Caddy ingress fronts the API by hostname, **but not the GUI, which stays plain HTTP on `:8443`** (see
[frontend-exposure.md](frontend-exposure.md); that mode is scaffolded, not live-verified). Semaphore, Grafana and
Prometheus serve HTTP on these ports in every mode. Once up:

| Surface | URL |
|---|---|
| Onboarding GUI (onboard-gui) | `https://${KONTROLL_MGMT_IP}:8443` — log in as `admin` / the `GUI_PASSWORD` kontroll-init showed |
| Semaphore | `http://${KONTROLL_MGMT_IP}:3001` |
| Grafana | `http://${KONTROLL_MGMT_IP}:3002` — `admin` / the Grafana password kontroll-init showed |
| API | `https://${KONTROLL_MGMT_IP}:8444` (read-only without `api_privileged` — see privileged-mutation-enablement.md) |

> **onboard-gui prerequisite:** deploy-stack provisions the GUI's repo clone and, in `self_signed` mode, mints its
> TLS certificate if none is there (the modes: [frontend-exposure.md](frontend-exposure.md)). It never mints the
> GUI's scoped **age key** (security boundary — a service that can create its own decryption key is not a trust
> boundary). Before `onboard-gui` will start, provision that key at
> `${KONTROLL_STORAGE_ROOT}/onboard-gui/age.key`. deploy-stack **fails
> closed** if that age key is absent rather than letting `up` auto-create a `root:root` directory in its place —
> and equally if it is present but the runtime uid cannot read it, which otherwise surfaces one deploy later as an
> opaque SOPS decrypt error. The simplest correct answer is a copy of the control key:
>
> ```bash
> sudo install -m 0600 -o 1001 -g 1001 ~/.config/sops/age/keys.txt /var/lib/kontroll/onboard-gui/age.key
> ```
>
> That is the accepted C10 residual — the GUI then decrypts the same domains the control node does. To give it a
> narrower identity instead, `age-keygen` a fresh key, add its **public** half to `instance/.sops.yaml`, re-wrap
> with `sops --config instance/.sops.yaml updatekeys instance/secrets/*.sops.yml`, and install the private half at
> the path above. The refusal prints this command, so you do not need to come back here for it.
>
> **Backup viewer (C14):** onboard-gui also bind-mounts the config-capture store (`${KONTROLL_BACKUPS_DIR}`)
> **read-only** at `/backups` so the GUI's **Backups** panel can index/view/diff captured device configs. This is
> a read of crown-jewel secrets, so the mount is `:ro` (never `:rw`) and the viewer redacts known secret shapes +
> audits every read (SECURITY.md C14). The feature is **inert** on a node with no captures yet — the panel shows
> "captures store not mounted", never an error — so no action is needed until you've run a backup.

> **Pre-provisioning storage (optional):** every at-rest store lives under `KONTROLL_STORAGE_ROOT` (default
> `/var/lib/kontroll`). Point it at a different disk to put the whole stack's state there, or override just the
> high-growth classes — `KONTROLL_LOGS_DIR` (Loki) and `KONTROLL_BACKUPS_DIR` (config captures) — to keep those
> on a roomy data disk while config stays on the system disk (`docker/.env.example` documents all knobs). Defaults
> preserve today's paths, so leaving them unset changes nothing. deploy-stack **creates each dir uid-scoped, at
> the mode SECURITY.md C8 gives it, before the stack comes up** (the chown follows the path you set); named
> volumes (Prometheus TSDB, Grafana, Postgres, Vector buffers) are Docker-managed and not relocated by these knobs.
> A root you make yourself (a mount, a directory on another disk) must be a directory of root's that neither its
> group nor others may write and that others may pass through, with every directory above it passable too —
> `sudo chown root:root <root>` and `sudo chmod 0711 <root>`; a deploy that brings up `api` or `onboard-gui`
> refuses another ([troubleshooting.md](troubleshooting.md) §9). A root you leave missing, the deploy makes so.

## 7. Distribute access + prove connectivity

- Push the control node's `ansible` SSH key to the SSH-managed hosts (Proxmox,
  docker hosts, OpenWrt); create API tokens for the API-managed ones (FortiGate,
  Proxmox). Reference them from the secrets files. When a class needs a specific
  token **scope/role** (e.g. Proxmox → `PVEAuditor`), the onboard form **surfaces
  that prerequisite for you** (from the module's `provisioning:` block) — you don't
  have to hunt a doc; the online-validate check later confirms the scope on the wire.
- Then:
  ```bash
  cd ansible && ansible-playbook playbooks/ping.yml   # per-class liveness
  ```
  Green = the control node reaches your fleet. (Hosts marked offline are tolerated.)

## Continual config (the "different person / next time" path)

- **New hardware class:** add `modules/<key>/` + a role, reference `<key>` in
  `instance/fleet.yml`, re-run the bootstrap. Nothing else.
- **Edge/core swap (e.g. cutover):** flip which modules are enabled in
  `instance/fleet.yml` (e.g. `opnsense` in, `fortigate` out) + move the inventory
  host, re-run bootstrap. Collections + roles converge.
- **Rebuild from scratch:** steps 0–6 above. The age key (step 5.2 backup) is the
  only non-reproducible secret — restore it and everything decrypts.
