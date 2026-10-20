# Installing kontroll from scratch on fresh Debian — the "Joe" path

> **Goal (operator directive):** every dependency + every step below is either **(A) AUTOMATED** by a
> script/playbook, or **(B) a SECURITY-BOUNDARY step** completable through **guided / GUI onboarding** (secret
> minting, key generation) — *never* by an agent, *never* committed in plaintext. Joe should reach a working
> instance by running a bootstrap + completing a guided onboarding, with **no hand-written commands beyond editing
> three overlay files, and no deep ops knowledge**. **Live-verified end-to-end 2026-06-15:** a blind install on a clean Debian 13 VM from the release
> bundle stands up the full core+GUI stack following only this doc (14 fresh-node blockers were found + fixed in
> the process — see CHANGELOG). The **[GAP]** tags mark steps that could be further automated/GUI'd.

> ### ⚡ Compose-native quickstart (Phase 1 — host floor = **Docker + git**)
> The install glue now runs in an **ephemeral installer container**, so the host needs only Docker + git — no host
> ansible-core / collections / sops / age (the version skew of which was this doc's fix(install) bug class). The
> rest of this page (§0–§8) is the **legacy host-ansible** path, still valid after `install-prereqs.sh
> --with-host-ansible`. **The block below is the one written install sequence:** every script's closing message
> and every other document points here instead of repeating it. From a launch kit, write `./kontroll` wherever it
> writes `scripts/kontroll-installer.sh`.
> ```
> # Get the tree and stand in it — one of:
> #   a clone:           git clone <repo> kontroll && cd kontroll
> #   a launch kit:      tar xzf kontroll-launch-<version>.tar.gz && cd kontroll-launch-<version>
> #   a release bundle:  mkdir src && tar xzf kontroll-<version>.tar.gz -C src     # the scripts are inside the bundle
> #                      sudo bash src/scripts/install-prereqs.sh                  # the next line needs git
> #                      bash src/scripts/install.sh kontroll-<version>.tar.gz && cd kontroll   # a local git tree, no remote
> sudo bash scripts/install-prereqs.sh                                  # Docker + compose + git + the C10 gid-1001 group
> #   If it says it added you to the docker group, log out and back in before the next line.
> scripts/kontroll-installer.sh fresh-init --mgmt-ip <ip> --domain <domain> --trust-mode separated   # scaffold instance/ + mint control key (§1–§2)
> $EDITOR instance/fleet.yml instance/inventory/hosts.yml instance/instance.yml
> scripts/kontroll-installer.sh check          # deploy-stack --check --diff (the mandated dry-run)
> #   On a node where nothing has ever been applied this is a PARTIAL preview and says so: --check does not
> #   create the canonical or the TLS certs, so the steps that clone from them or take ownership of them are
> #   skipped. Everything else is previewed, and it still fails on REAL preconditions you have not met yet
> #   (e.g. the onboard-gui age key) — which is the point of running it before the apply.
> scripts/kontroll-installer.sh                # bootstrap → deploy-stack: local canonical + the stack (§3–§5)
> scripts/kontroll-installer.sh configure-semaphore                      # wire Semaphore (§6)
> ```
> That is Semaphore and Homepage, with the local canonical behind them. **The onboarding GUI is a separate,
> deliberate step — §7:** its age key is yours to provision, and §7's deploy brings it up together with the
> staging API (`api_privileged`). Until then nothing listens on `:8443`.
>
> **Running a play later.** On this path a control-plane play — `ansible-playbook playbooks/<x>.yml <args>` for
> `bootstrap.yml`, `local-canonical.yml` or `reload-observability.yml` in this repository's documents — is run as
> `scripts/kontroll-installer.sh playbooks/<x>.yml <args>` (the path is relative to `ansible/`); a
> `deploy-stack.yml <args>` is `scripts/kontroll-installer.sh init <args>`; and
> `python3 scripts/configure-semaphore.py` is `scripts/kontroll-installer.sh configure-semaphore`. A play against
> fleet devices (`ping.yml`, `backup-configs.yml`, …) is run as its Semaphore task: the device collections are in
> the runner image, not in the installer's.
>
> Full reference + the security model: [docker/installer/README.md](../docker/installer/README.md). Each §N below
> maps to a `kontroll-installer.sh` verb, so the security boundaries (the **[B]** GUI-onboarding steps) are unchanged.

## 0. System dependencies — **[A]** `scripts/install-prereqs.sh` (idempotent; `./bootstrap.sh` shims it)

`sudo bash scripts/install-prereqs.sh` installs the host floor in one idempotent pass — Docker, compose and git;
`--with-host-ansible` adds the rest of the table (the legacy host toolchain). It is
**nixflavor-agnostic**: it auto-detects your package manager (apt/dnf/zypper/pacman) and installs the host
floor accordingly — on apt it uses Docker's deb822 repo (matching the image + deploy-stack), elsewhere it uses
Docker's official cross-distro installer (`get.docker.com`). Everything beyond the floor runs in the installer
container, so only the host *floor* is distro-specific. A fresh control node needs, before anything:

| Package | Why | Notes |
|---|---|---|
| `git`, `curl`, `openssl` | clone, health checks, token/cert generation | base |
| `docker-ce` + `docker-compose-plugin` | the whole stack runs in compose | from Docker's apt repo |
| `python3`, `python3-pip`, `python3-venv` | the service layer + generators + galaxy CLI | 3.11–3.14 supported |
| `ansible-core` **(>=2.16)** + collections `community.sops`, `community.docker`, `community.general` | the playbooks (deploy-stack, local-canonical, bootstrap) | **NOT on the host by default** — these live in the installer image, which is the whole point: no host toolchain means no version skew, and the PR #87 fresh-Debian-12 bug class is gone structurally. `install-prereqs.sh --with-host-ansible` (legacy rollback) installs them on the host, pip-installing ansible-core because Debian's apt `ansible` is core **2.14** — too old for `deb822_repository` (needs >=2.15) AND its bundled dist-packages collections shadow the lockfile pins. |
| **`sops`** + **`age`** | secrets at rest (SOPS+age) | **NOT on the host by default** — also in the installer image. `--with-host-ansible` installs them (`age` via apt, `sops` from its pinned GitHub release). Run sops through the installer (`scripts/kontroll-installer.sh shell`) rather than expecting a host binary. |
| **`acl`** | ansible `become_user`'s unprivileged-become path needs `setfacl` | **HIT LIVE** — without it, `become_user: 1001` fails `chmod: invalid mode 'A+user:...'`. deploy-stack now AVOIDS `become_user` (its git in an armed content clone is started through `setpriv` as uid 1001), so `acl` is optional — but install it to be safe. |
| `yamllint`, `ansible-lint` | `tests/validate` (the lint gate) | dev only; ansible-lint needs POSIX (won't run on Windows — no `grp`) |
| `gitleaks` | secret-scan (validate + CI) | dev/CI only |
| `prom/prometheus` image | `promtool check config` runs via the image | **no host install** — `docker run --rm --entrypoint promtool ...` |

## 1. Get the code + scaffold YOUR overlay — **[A]**

Get the tree as the quickstart's first lines do — a clone, a launch kit or a release bundle — stand in it,
and scaffold a private `instance/` overlay with your own keys, never the tool author's:

> **Choosing the two values:** `--mgmt-ip` is the **static** IP of this node's management interface — the address
> you'll browse the GUI on (`https://<ip>:8443`), so assign it statically first. `--domain` is **cosmetic** under
> the default `self_signed` TLS — pass any placeholder (e.g. `kontroll.lan`) or omit it. Both are editable later
> in `instance/instance.yml`, so a wrong guess is recoverable.

```bash
python3 scripts/kontroll-init.py --fresh --mgmt-ip <your-mgmt-ip> --domain <your-domain> --trust-mode separated
```

> `--trust-mode separated` puts a human review gate on every staged proposal (the recommended posture). What
> `solo` does, and what `--fresh` does when the flag is left out, depends on the edition you are installing — read
> [trust-mode.md](trust-mode.md) before choosing anything else.

`--fresh` copies `instance.example/` to `instance/` (your keys only — it never inherits
another instance's recipients), mints your control age key, seeds `instance/.sops.yaml`,
fills `instance/instance.yml`'s `mgmt_ip`/`domain` **and the Homepage portal tiles** (so a
fresh `:3000` shows your real control-plane services — onboard-GUI/Semaphore/Grafana/
Prometheus/API — not the TEST-NET stubs), and mints the bootstrap logins.

> ⚠️ **`--fresh` prints generated passwords (onboard-GUI, Grafana, Semaphore admin) ONCE, then exits — copy the
> whole output to your password manager now.** Your GUI login at §8 is the field printed as **`gui_admin_password`**
> (the onboard-GUI `admin` password). To recover any of them later:
> `SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt sops -d instance/secrets/dashboards.sops.yml`.

Then declare your hardware in the overlay **under `instance/`** (not `config/` or `instance.example/`):

- `instance/fleet.yml` — for each "pick ONE" group, keep exactly one module enabled. Every line of the list is a
  device-class key; the bootstrap checks that first and stops on one that is not
  ([troubleshooting.md](troubleshooting.md) §6).
- `instance/inventory/hosts.yml` — real mgmt IPs, **or leave the `192.0.2.x` placeholders for now** and add real
  devices later via GUI onboarding (§8); the stack still comes up device-less.

## 2. Keys + all secrets — **[B] SECURITY BOUNDARY → guided onboarding** (the first control key + GUI_PASSWORD are `kontroll-init --fresh`; everything else is the GUI Keys/Secrets dialogs)

> **During install you do almost nothing here — `kontroll-init --fresh` (§1) already minted your control key and
> bootstrap logins.** The break-glass key, the scoped-Semaphore key, and all device/service secrets are entered
> **later, through the GUI** (§8). On the compose-native path there is nothing to do now — `init` generates the
> fleet SSH key as well; on the legacy host path generate it (the bullet below). The rest of this section explains
> *why* the boundary exists — skim it and continue to §3.

These are the steps an agent must NEVER do and that must NOT be committed in plaintext. They are the prime
candidates for a **guided key-gen + secret-entry wizard** (generate-on-box, encrypt-on-box, show-once for the
offline break-glass backup):

- **age keys** (`age-keygen`): the **control-VM key** (`~/.config/sops/age/keys.txt`), the **offline break-glass
  key** (shown once, stored offline), and the **scoped Semaphore key** (`~/.config/semaphore/age/keys.txt`,
  recipient of `network`+`proxmox` only). `.sops.yaml` already encodes the per-domain recipiency.
  **[Resolved — the GUI "Keys" dialog]:** the **break-glass**, **scoped-Semaphore**, and **control-rotation**
  keys are now minted through the GUI (`key-roles/<role>.yml` → `/api/keygen`): generate-on-box, the **private
  key shown once** (the operator stores it — the service never persists it), the **public** recipient added to
  `instance/.sops.yaml` additively + parse-verified + staged (C10), and `sops --config instance/.sops.yaml
  updatekeys instance/secrets/*.sops.yml` returned as a deferred step.
  The **first control key + the first `GUI_PASSWORD`** are a CLI **`kontroll-init`** bootstrap (a running GUI
  can't mint the key it needs to run — the keygen split): `python3 scripts/kontroll-init.py` ensures the
  control key, adds its public to `.sops.yaml`, and mints the `dashboards` bootstrap secrets
  (`GUI_PASSWORD`/Grafana/API token, shown once). See SECURITY.md **C11**.
- **SOPS domains** (`instance/secrets/<domain>.sops.yml`) — each VALUE minted by the operator, encrypted on the
  box: `network` (device creds), `proxmox` (read-only API token), `compute` (docker-host SSH), `dashboards`
  (Grafana admin pw, GUI pw, **`kontroll_api_token`** for C10), `semaphore` (DB/admin pw, access-key
  encryption), `snmp_observability` (the SNMPv3 v3 read user). A new domain gets `base_recipients`
  (control+break-glass) via the `.sops.yaml` catch-all.
- **SSH keys** (the fleet/device SSH key, the GitHub deploy key) — generated on the box. On the compose-native path
  `init` generates the fleet key for you (`bootstrap.yml`); on the legacy host path **generate it now** —
  `configure-semaphore` (§6) reads it, and stops without it:
  ```bash
  ssh-keygen -t ed25519 -f ~/.ssh/ansible_ed25519 -N "" -C "kontroll-fleet"
  ```

> **[Resolved — the operator's main ask]:** every secret-minting + key-gen step is now a **GUI onboarding form**
> so Joe enters/generates creds in the browser and the box encrypts them — the agent never sees a value. Device
> creds: `gui/app.py` `/api/onboard` (`--username/--password/--api-token` → SOPS). **Service/infra secrets:** the
> **"Secrets"** dialog (`/api/secrets`, `secret-forms/<domain>.yml`). **age keys:** the **"Keys"** dialog
> (`/api/keygen`, `key-roles/<role>.yml`) for break-glass/Semaphore/rotation. The only step that stays a CLI
> bootstrap is the **first** control key + `GUI_PASSWORD` (`kontroll-init`, item E) — a running GUI can't mint
> the key it needs to run. See SECURITY.md **C11**.

## 3. The local canonical `/srv/kontroll.git` — **[A]** `ansible-playbook playbooks/local-canonical.yml`

Bare repo + HEAD→main + the working tree's `local` remote + the landing of the working tree's HEAD on `main`
(`scripts/kontroll-land.py`: judged by the promote gate first — its `--check` runs under the dry-run too — then a
pack on a pipe and one compare-and-swap; never a push, which the canonical's hook refuses for `main` from everyone;
SECURITY.md C10). Now also (C10): sets
`receive.denyNonFastForwards`/`denyDeletes` + the top-dir group-1001 write grant (setgid 2775).

> **[Resolved]:** the **recursive write-grant on EXISTING objects** (a canonical created before the gid-1001
> grant) is now an **idempotent `local-canonical.yml` detect-then-apply pair** — the ownership pass of
> `scripts/kontroll-canonical.py --normalise`, which makes every entry **`<operator>:1001`**, group-writable and
> (a directory) setgid, and runs only when its `--check` named something (a second run reports `0 changed`; the
> dry-run prints the same names). It walks the canonical as the audit reads it — every entry opened from the
> directory it was listed in and changed through its own descriptor; a symbolic link is named and left as it is,
> and nothing it points at is touched ([troubleshooting.md §8](troubleshooting.md#8-the-audit-of-the-canonical-named-something)).
> On a *fresh* canonical the setgid handles it; on an existing one the pass does. No manual `chown -R / chmod -R`
> step. `hooks/` is passed over by the pass and kept **root:root 0755**: the uid-1001 writer
> that holds the canonical `:rw` must never be able to plant a hook that the next promote runs as root (SECURITY.md C10).
>
> **[Resolved — F-CANON, #119]:** the gap that bit the prod cutover is also closed in the same playbook: on a
> fresh node there was **no named group at gid 1001 and the operator was not a member**, so the operator's Mirror
> push died `Permission denied`. `local-canonical.yml` now **creates the `kontroll` group (gid 1001) and adds the
> operator** before touching the canonical, and the recursive grant sets **owner=operator** — so the operator's
> push succeeds via OWNER perms even on the very first run (a freshly added supplementary group isn't effective
> until the operator's next login). No manual `groupadd / usermod / chown` step. Pinned by
> `tests/unit/test_local_canonical.py`.
>
> **[Resolved — F-CANON, the in-container path, `task_51334deb`]:** `local-canonical.yml` **skips** the group +
> membership tasks when it runs *inside the installer container* (`KONTROLL_IN_CONTAINER=1` — both the published-kit
> and local-build deploys), because a container-local group dies with the ephemeral installer. So a published-kit
> install used to leave the host group unprovisioned and the operator's later **promote** failed `Permission denied`
> deleting the consumed `proposed/<run_id>` ref. Now the **host floor** owns the gesture: `install-prereqs.sh`
> (root) creates the group + adds the operator, and the launchers (`kontroll.sh` / `kontroll-installer.sh`)
> **ensure-or-warn** at run time (auto-provision via sudo if available, else print the exact one-liner loudly).
> Shared, idempotent helper: `scripts/lib/canonical-group.sh`; pinned by
> `tests/unit/test_canonical_group_provisioning.py`. SECURITY.md C10.

## 4. Runner image + collections — **[A]**

`deploy-stack.yml` builds the `kontroll/semaphore` image (ansible + sops + age + baked collections +
flask/fastapi/uvicorn + the **C10 git identity**). Collections derive from `modules/` via `gen-requirements`
(never a hand-listed set). `bootstrap.yml` installs only the enabled device classes' collections + roles.

## 5. Deploy the stack — **[A]** `ansible-playbook playbooks/deploy-stack.yml`

Run the **bare** command (from `~/kontroll/ansible`):

```bash
cd ~/kontroll/ansible
ansible-playbook playbooks/deploy-stack.yml
```

It brings up the **default set — `semaphore` + `homepage`** (the live-usable core). It also **regenerates the
runner's collection lockfile from your enabled modules and builds the runner image itself**, so you do **not**
need to run `bootstrap.yml` first for a basic stand-up. Renders `docker/.env` from SOPS (`no_log`), seeds the
local canonical, brings up the services.

> Add more services later by naming them explicitly, e.g. once their secrets/keys are onboarded:
> `ansible-playbook playbooks/deploy-stack.yml -e '{"stack_services":["semaphore","homepage","prometheus","grafana"]}'`.
> The `onboard-gui` GUI + the exporters come up in §7–§8 (they need an age key / device creds first). **Do not**
> pass `-e @/tmp/sv.yml` — that path is an internal GUI-enact artifact, never something you create by hand.

## 6. Semaphore project — **[A]** `python3 scripts/configure-semaphore.py` (run **after** the §5 deploy)

> **Order:** `configure-semaphore.py` talks to the **running** Semaphore API on `${KONTROLL_MGMT_IP}:3001` that
> the §5 deploy-stack just brought up — so it follows §5.

Idempotent: keys, repo (`file:///srv/kontroll.git`), env, inventory, the Semaphore **templates** (including the
**`promote-proposal`** template §7 drives), and the **per-class backup schedules** (generated from each `backup:`
block via `gen-backup.py` → `config/semaphore/schedules.generated.yml`, not a hand-listed hub). For the admin password it
reads `SEMAPHORE_ADMIN_PASSWORD` from the env, else **self-decrypts** it from the `semaphore` SOPS domain
(`instance/secrets/semaphore.sops.yml`) with the control node's age key — so on the control node you run it with
**no manual export** (value never printed). To override (or run off-node), export it first:
`export SEMAPHORE_ADMIN_PASSWORD=$(SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt sops --extract
'["semaphore_admin_password"]' -d instance/secrets/semaphore.sops.yml)`. On a brand-new node the `github-deploy`
key and the scoped `SOPS_AGE_KEY` env-secret are skipped (no offsite remote / the scoped Semaphore key is minted
later via the GUI 'Keys' dialog) — re-run this after minting the scoped key to inject it.

## 7. C10 privileged-mutation enablement (live actuation) — **[A] gated**

This is what lets the GUI/API actually **stage** a change (push `proposed/<run_id>`) so a trusted promote can land
it on `main`. Verified live 2026-06-15/18. See [privileged-mutation-enablement.md §5](privileged-mutation-enablement.md)
for the full recipe; the itinerary:

1. **The C10 API token is already minted — nothing to do here.** `kontroll-init --fresh` (§2) generated
   `kontroll_api_token` into the `dashboards` SOPS domain (alongside the GUI/Grafana passwords); the gated deploy
   renders it from there. *(To **rotate** it later, use the GUI "Secrets" form — the `dashboards` domain's
   `kontroll_api_token` field, blank submit re-`generate: hex32`; the value never crosses argv or a temp file.
   Avoid `sops --set` / `openssl rand` in a shell — SECURITY.md C11.)*
2. **[A]** The recursive canonical write-grant (§3, resolved) + `acl` (§0, if `become_user` is ever reintroduced).
3. **Provision the onboard-gui age key** — its trust grant (the GUI encrypts onboarded device creds with it; the
   deploy never does this for you, the security boundary):
   ```bash
   sudo install -d -m 0755 /var/lib/kontroll/onboard-gui
   sudo install -m 0600 -o 1001 -g 1001 ~/.config/sops/age/keys.txt /var/lib/kontroll/onboard-gui/age.key
   ```
4. **[A] Arm all three privileged surfaces in one deploy:**
   ```bash
   cd ~/kontroll/ansible
   ansible-playbook playbooks/deploy-stack.yml \
     -e '{"stack_services":["api","onboard-gui","semaphore"],"api_privileged":true}'
   ```
   This renders the token + `KONTROLL_STAGE_PUSHES=1`, has uid 1001 make (or update) the `api`/`onboard-gui`
   content clones — a root-owned one is removed and made again, never chowned — adds each a `local` remote, runs them as `user: "1001:1001"`, and renders `KONTROLL_CANONICAL_MODE=rw` so all
   three mount `/srv/kontroll.git` `:rw`. `api` + `onboard-gui` can then only **PROPOSE** (`proposed/<run_id>`);
   `semaphore` is the one that **promotes** — so all three are named together and **recreated** in this one deploy
   (found live: omitting `semaphore` leaves it mounting the canonical `:ro`). `local-canonical.yml` (§3) set
   `core.sharedRepository=group`, so a promote writes as **gid 1001**, no host root.

**Promote = a SEPARATE Semaphore web app (the two-key safety).** Under `trust_mode: separated` a proposal is
landed on `main` from a **different app, with a different login**, than the GUI that created it — so a leaked API
token can only park rejectable proposals, never rewrite the canonical (SECURITY.md C10;
[trust-mode.md](trust-mode.md) says what `solo` changes). To promote a `run_id`:

1. Open Semaphore at **`http://${KONTROLL_MGMT_IP}:3001`** and log in **`admin` / `semaphore_admin_password`**
   — the §2 value, **not** the GUI's `gui_admin_password` (a deliberately separate credential).
2. Open the **`promote-proposal`** template (registered by §6) → **Run** → in the survey, enter the **`run_id`**
   from the GUI's propose/onboard response → **Run**.
3. It **fast-forward-merges** `proposed/<run_id>` into `main` (FF-only — a proposal can never rewrite history) and
   deletes the proposal. Before that it resolves the tree the promote would make `main`: one the collection
   generator refuses — two units pinning one collection differently, a fleet line that is not a class key — is
   refused (exit 2) with a sentence that says what to correct. *CLI fallback:* `sudo python3
   scripts/kontroll-promote.py <run_id> --repo /srv/kontroll.git`.
   The promote is reachable only by a Semaphore admin on the mgmt VLAN — **never the API token**.

## 8. Use it — onboarding + capabilities (the guided flow)

The GUI came up **and was armed for staging** in §7. Open it at **`https://${KONTROLL_MGMT_IP}:8443`** — accept
the **self-signed cert warning** (the local mgmt cert) — and log in **`admin` / `gui_admin_password`** (the value
kontroll-init showed once in §1; **not** the Semaphore password). Then the guided loop runs in this order:

1. **Onboard a device.** **Search** a collection → **Onboard this** → enter the device IP + creds → **Run** (tick
   *apply*). Behind the form: the creds are SOPS-encrypted, the module + inventory drop-in + fleet-enable are
   written, and the change is **staged** as `proposed/<run_id>` (never `main`). The `run_id` is in the result.
   *(Onboarding a device of a class that already exists **reuses** that class — it adds only the host, #124.)*
2. **(optional) Add a secondary capability.** Click a **telemetry / backup / logging** badge → pick a method →
   **Propose** (a pure plan + an anti-drift token) → **Promote** (also staged as `proposed/<run_id>`).
3. **Promote** each `run_id` in the **Semaphore web app** — the §7 *Promote* walkthrough (`promote-proposal`
   template, the `run_id` survey field). (An edition that runs an auto-promoter does this step itself under
   `solo`, for what its scope allows — [trust-mode.md](trust-mode.md).) This fast-forwards the proposal onto
   `main`; for a plain device onboard, the device is now in the inventory + fleet.
4. **Enact** ("make it live") splits by privilege (item F): **Tier-1** steps — the Prometheus reload
   (`reload-observability`), a host-agent install (`install-node-exporter`) — are **one-click Semaphore tasks**
   (no host root); **Tier-2** `deploy-stack` (bring up an exporter container / fetch dashboards / rebuild the
   runner) is an **operator command the GUI shows**, because it needs host Docker. After a telemetry enact the
   capability dialog surfaces a **clickable Grafana deep-link** (#122). Nothing gates onboarding (INVARIANT D\*).

> **The GUI promote path** is served by **onboard-gui** (Flask) over the same `gitio` staging mechanism as the
> API — §7's arm provisioned its writable clone identically (`user: "1001:1001"`, the `/srv/kontroll.git` mount,
> a clone made by uid 1001, a `local` remote) — so a capability-promote from the GUI stages `proposed/<run_id>` (never
> `main`), exactly like the API. The age key stays host/GUI-provisioned (the security boundary); the self-signed
> certificate is minted by the deploy.
> (**Live-verified 2026-06-15/18:** the gated deploy provisioned the clone; a `run_id` push staged
> `proposed/<run_id>` with `main` untouched; the `promote-proposal` task FF-merged it onto `main`.)

## Dependency / automation summary (what to build to make this one-command + guided)

| To automate/GUI | Current state | Target |
|---|---|---|
| System prereqs (incl. Docker) | ~~manual apt~~ | ✓ `scripts/install-prereqs.sh` (idempotent; `./bootstrap.sh` shims it) |
| age key-gen (break-glass / Semaphore / rotation) | ~~manual `age-keygen`~~ | ✓ GUI "Keys" dialog (`/api/keygen`, show-once private, additive `.sops.yaml`, staged) |
| first control key + `GUI_PASSWORD` | ~~manual `age-keygen` / `openssl`~~ | ✓ CLI `scripts/kontroll-init.py` — the bootstrap a running GUI can't do |
| All service/infra secrets | manual `sops` | **per-domain GUI onboarding forms** (the main ask) |
| Recursive canonical write-grant | ~~manual `chgrp/chmod`~~ | ✓ idempotent `local-canonical.yml` detect-then-apply pair (`kontroll-canonical.py --normalise`) |
| C10 token | ~~manual `sops --set`~~ | ✓ guided secret form (`dashboards.kontroll_api_token`, `generate: hex32` — no value in argv, C11) |
| Promote | ~~manual `sudo kontroll-promote.py`~~ | ✓ `promote-proposal` Semaphore task (run_id survey var; FF-only; gated rw canonical mount, gid-1001, no host root) |
| onboard-gui staging | gated deploy provisions its clone-remote (uid:gid + mount + remote) | ✓ done |
| `acl` | manual (or avoided) | `install-prereqs.sh` |

## Troubleshooting

### A container build/pull can't resolve DNS (fresh Debian + a locked-down mgmt VLAN)

**Symptom:** during §4/§5 the runner-image build stalls or fails — `ansible-galaxy` collection installs time out, an
image pull reports it "could not resolve host", or container logs show DNS lookup failures — even though the **host**
itself resolves names fine (`getent hosts github.com` works).

**Cause:** on a fresh Debian host, `systemd-resolved` publishes a `127.0.0.53` stub resolver in `/etc/resolv.conf`.
That loopback address is unreachable from inside a container's network namespace, so Docker falls back to its built-in
default of **`8.8.8.8`** for container DNS. A locked-down management VLAN commonly **blocks outbound to public
resolvers**, so containers get no working DNS while the host is unaffected.

**Fix:** point the Docker daemon at a resolver the containers can actually reach (your LAN/mgmt gateway or an internal
resolver), then restart Docker:

```bash
# if /etc/docker/daemon.json already exists, MERGE the "dns" key instead of overwriting the file
echo '{"dns":["<your-lan-resolver-ip>"]}' | sudo tee /etc/docker/daemon.json
sudo systemctl restart docker
```

Replace `<your-lan-resolver-ip>` with your router / internal DNS (e.g. the mgmt-VLAN gateway). This only bites on
networks that block public DNS — on an unrestricted network Docker's default resolves fine, so kontroll does **not**
set this for you (a forced default would override a correct config and could clobber an existing `daemon.json`).

### The deploy, or `./kontroll <verb>`, stops with `REFUSING to commit` / `refusing to capture the tree`

**Symptom:** `local-canonical.yml` (every deploy runs it) fails with
`local-canonical: REFUSING to commit — plaintext-guard: REFUSED <path> — <rule>`, or the launch-kit launcher prints
`plaintext-guard: REFUSED <path> — <rule>` and `kontroll: refusing to capture the tree`. That run staged, committed
and pushed nothing. (The same lead followed by `gen-requirements: instance/fleet.yml: …` is the other check in
front of that commit — a fleet line that is not a device-class key: [troubleshooting.md](troubleshooting.md) §6.)

**Cause:** the deploy commits your whole working tree into the local canonical, and the launcher captures the kit
tree the same way. Just before either, `scripts/lib/plaintext-guard.sh` reads what would be committed and refuses a
file that would put a secret there in the clear. The rule says which kind:

| Rule | What it found | Fix |
|---|---|---|
| `not-sops-encrypted (no sops block)` | a `*.sops.yml` that was written in an editor and never encrypted | `sops --config instance/.sops.yaml -e -i <path>`. A launch-kit host has no sops: run `./kontroll shell` first (it still works after a refusal) and use the image's |
| `not-sops-encrypted (line N)` | an encrypted file with something on line N that sops did not write — a key added by hand (also at the very end of the file, where the new line becomes part of the `sops:` block), a plaintext comment | remove the line, then add the value through the GUI's Secrets dialog, or with `sops --config instance/.sops.yaml <path>` on a machine that has sops and an editor. On a launch-kit host: `./kontroll shell`, then `sops --config instance/.sops.yaml set --value-stdin <path> '["<key>"]'`, type the value as JSON (`"<value>"`, with the quotes) and end the input with Ctrl-D — the image has no editor, `set` needs none, and a value read from the input is not left in the shell's history. If line N is a key that contains `: `, or one too long for a single line (sops writes it as `? key`), rename the key |
| `not-sops-encrypted (unreadable)`, `private-key-material (unreadable)` | a file the deploying user cannot read | fix its owner or mode |
| `secrets-dir-member` | a file in `instance/secrets/` (or the legacy `ansible/secrets/`) that is not a `*.sops.yml`, `README.md` or `.gitkeep` — a decrypted copy, an editor backup | delete it, or move it out of the repository |
| `private-key-material (line N)` | a file that holds an age secret key, a line that begins with a PEM or PGP private-key header, or such a header inside a string or a YAML block with the key directly after it (an SSH key inlined in a vars file). A document that quotes a header and begins its next line with a long unbroken word (a digest, a path) is refused too | move it out of the repository; a key never belongs in it. A key Ansible needs goes into a sops-encrypted secrets file. In the document, break the line elsewhere |
| `unusual-file-name` | a file whose name contains a line break | rename it |
| a sentence ending `— refusing`, with no `REFUSED` | the guard could not look, so nothing was checked: git cannot read the directory as a worktree (git's own message is printed above — "dubious ownership" means the tree belongs to another user), the directory is not the top of its git repository, the list of files did not arrive whole, or the check itself did not finish | fix what the sentence names, then re-run |

The guard reports the path, the rule and line numbers — never the content. It only reads files the commit would
take (new, modified or already staged), so a clean tree at the top of its own repository is never refused. If the file is one you want to keep and
it is not a secret, it is in the wrong directory: `instance/secrets/` holds sops documents and nothing else.
After a refusal `./kontroll shell` is the one launcher verb that still runs: it deploys nothing, so it captures
nothing.

A partial-encryption rule in `.sops.yaml` (`unencrypted_suffix`, `encrypted_regex`) leaves values in the clear on
purpose; the guard refuses a value it leaves in the clear like any other plaintext. The shipped creation rules
encrypt every value.

## See also
- [SETUP.md](SETUP.md) · [bootstrap-control-vm.md](bootstrap-control-vm.md) — the existing setup docs
- [privileged-mutation-enablement.md](privileged-mutation-enablement.md) — C10 decision + the verified recipe
- [../SECURITY.md](../SECURITY.md) — C1 (secrets), C8/C9/C10 (the privileged surfaces + trust boundaries)
