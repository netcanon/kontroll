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

The things Ansible can't install before it runs (Ansible, Python) and the one no later step installs
(Docker — the whole stack runs in compose):

```bash
./bootstrap.sh            # → scripts/install-prereqs.sh: Docker + compose + git (the host floor)
```

Idempotent (safe to re-run). `bootstrap.sh` is now a thin shim over
[scripts/install-prereqs.sh](../scripts/install-prereqs.sh) — run either.

## 2. Get the code onto the node

**Git-free (recommended for an operator):** install from a release bundle — **no GitHub
account or clone needed.** On a machine with the code, `bash scripts/make-bundle.sh`
produces `dist/kontroll-<version>.tar.gz`; copy it to the node (scp/USB/any transfer) and:

```bash
bash scripts/install.sh kontroll-<version>.tar.gz     # git-init a local tree, no remote
cd kontroll
python3 scripts/kontroll-init.py --fresh --mgmt-ip <your-mgmt-ip> --domain <your-domain> --trust-mode separated
```

`--trust-mode` is **required** with `--fresh` (kontroll never silently picks your promote posture): `separated`
(a human promotes each staged proposal — recommended/prod) or `solo` (homelab auto-promote; the auto-promoter ships
in a later release — until then it behaves like `separated`). See [trust-mode.md](trust-mode.md).
`kontroll-init --fresh` scaffolds your private `instance/` overlay from the shipped
`instance.example/` skeleton (your keys only — it never inherits the bundle author's
recipients), mints your control age key, seeds `instance/.sops.yaml` with that key, fills
`instance/instance.yml`'s `mgmt_ip`/`domain` **and the Homepage portal tiles** (a fresh
`:3000` shows your real control-plane services, not the shipped TEST-NET stubs), and mints
the `dashboards` bootstrap login secrets (shown once — save them). The instance is the source of truth
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

## 3. Declare your fleet + your hosts

Edit your overlay (scaffolded by `kontroll-init --fresh` in §2):
- **`instance/fleet.yml`** — enable the modules you actually run (catalog in
  `config/fleet.example.yml`). This is the file most setups touch.
- **`instance/inventory/hosts.yml`** — your hosts, in functional groups
  (`edge_firewall`, `core_switch`, …).
- **`instance/instance.yml`** — confirm `mgmt_ip` / `domain` (set by `--fresh`) and
  pick a `frontend.tls_mode`.

## 4. Stage 1 — run the bootstrap

```bash
cd ansible        # from the install dir (kontroll/)
ansible-playbook playbooks/bootstrap.yml
```

This is idempotent and does everything we used to do by hand:
- generates `requirements.generated.yml` from `_core` (what the install playbooks import, whatever the fleet)
  + your enabled modules via the
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
`instance/.sops.yaml` with your key only rather than appending to an existing overlay.)
It ensures the control age key, adds its public half to `instance/.sops.yaml` (additive +
parse-verified), and mints
the onboard-GUI password (`GUI_PASSWORD`), the Grafana admin password, and the C10 API token into the
`dashboards` SOPS domain — **showing the login passwords once** (save them). Then do the break-glass key
(step 2 below) and the remaining service secrets (the GUI **"Secrets"** dialog after deploy, or `sops`).
The manual equivalents:

1. Put the age recipient the bootstrap printed into **`instance/.sops.yaml`** (replace the
   placeholder). *(kontroll-init does this.)*
2. **Set up a break-glass recovery key.** (The `kontroll-init` fast path above already
   minted the `dashboards` bootstrap secrets to your control key, so add the break-glass
   recipient now and **re-wrap** them with the `updatekeys` command below — that is what
   makes them recoverable retroactively. Do it before creating any *further* secrets.)
   On a **trusted machine (not the control node)**, generate a second age key and
   store its **private** half offline (password manager + paper) — it must never
   touch the control node:
   ```bash
   age-keygen -o kontroll-breakglass.key      # prints "# public key: age1..."
   ```
   Add **both** recipients (control-node + break-glass) to `instance/.sops.yaml` so every
   secret encrypts to both; then re-wrap with the overlay config:
   `sops --config instance/.sops.yaml updatekeys instance/secrets/*.sops.yml`.
   Now losing the control-node key is *recoverable* (the offline key decrypts
   everything) instead of catastrophic. Also keep a copy of the control-node key
   (`~/.config/sops/age/keys.txt`) in your password manager.
3. Create the per-domain secret files the bootstrap listed. Prefer the GUI **"Secrets"**
   dialog after deploy (the value is encrypted on the box, never in your shell history);
   the CLI fallback is:
   ```bash
   sops instance/secrets/network.sops.yml     # device creds / API tokens
   sops instance/secrets/proxmox.sops.yml      # PVE API token
   ```

## 6. Deploy the stack and open the GUI

```bash
cd ansible
ansible-playbook playbooks/local-canonical.yml     # seed the local canonical the instance runs from
ansible-playbook playbooks/deploy-stack.yml        # brings the composed stack UP (renders docker/.env from instance/instance.yml + SOPS)
python3 ../scripts/configure-semaphore.py          # wire Semaphore — RUN AFTER deploy (it talks to a RUNNING Semaphore on :3001)
```

The stack binds every privileged port to your `mgmt_ip` only (never 0.0.0.0). Once up:

| Surface | URL |
|---|---|
| Onboarding GUI (onboard-gui) | `https://${KONTROLL_MGMT_IP}:8443` — log in as `admin` / the `GUI_PASSWORD` kontroll-init showed |
| Semaphore | `http://${KONTROLL_MGMT_IP}:3001` |
| Grafana | `https://${KONTROLL_MGMT_IP}:3002` — `admin` / the Grafana password kontroll-init showed |
| API | `https://${KONTROLL_MGMT_IP}:8444` (read-only until armed — see privileged-mutation-enablement.md) |

> **onboard-gui prerequisite:** deploy-stack provisions the GUI's repo clone and TLS
> *directory* but does NOT mint its TLS cert or scoped age key (security boundary — a service that can create its
> own decryption key is not a trust boundary). Before `onboard-gui` will start, host-provision a self-signed cert
> at `${KONTROLL_STORAGE_ROOT}/onboard-gui/tls/{gui.crt,gui.key}` and the scoped age key at
> `${KONTROLL_STORAGE_ROOT}/onboard-gui/age.key` (see docs/frontend-exposure.md). deploy-stack **fails
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
> preserve today's paths, so leaving them unset changes nothing. deploy-stack **creates each dir uid-scoped 0750
> before the stack comes up** (the chown follows the path you set — SECURITY.md C8); named volumes (Prometheus
> TSDB, Grafana, Postgres, Vector buffers) are Docker-managed and not relocated by these knobs.

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
