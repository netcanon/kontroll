# kontroll — Security Architecture

Security model, threat assumptions, implemented controls, and accepted risks for
the control plane. Modeled on the NetConfig project's SECURITY.md. **Update this
document whenever a security-relevant change is made** (triggers in the last
section).

> Status (as of Phase 1, 2026-06-13): secrets-at-rest wiring (age key + `.sops.yaml`
> recipient) and repo hygiene are in place; device-credential files are pending.
> Rows are marked ⬚ planned / ◑ partial / ✔ implemented. Each control names the
> file that implements it and the check that proves it — security claims without
> a covering check are not claims.

---

## Reporting a vulnerability

Report security issues **privately** through GitHub's private vulnerability reporting for this
repository (*Security → Report a vulnerability*, which opens a draft advisory only the
maintainers can read). Never open a public issue or pull request for a vulnerability. You
will get an acknowledgement within a few days; fixes ship as a normal release with a credit
in `CHANGELOG.md` unless you ask otherwise. Supported: the current `main` and the latest
`v*` release. Out of scope: the managed devices and third-party images themselves (report
those upstream), and findings that require an already-compromised control node.

---

## Threat model

kontroll is a **single-user homelab control plane** running on a dedicated VM on
the Proxmox cluster, on the management VLAN. It holds standing credentials
to every managed device — so **encryption at rest is mandatory here.**

| Actor | Trust level |
|---|---|
| The operator (you) on the control VM | Fully trusted |
| Other hosts on the mgmt VLAN | Semi-trusted — reachable, but not granted control creds |
| Hosts on the other VLANs | Untrusted toward the control plane; reached only outbound, scoped |
| WAN / Internet | Out of scope — the control plane is never WAN-exposed |

---

## Controls

### C1 — Secrets encrypted at rest (SOPS + age)  ◑ partial

**Implements:** `.sops.yaml` (real control-VM recipient + per-domain rules), age
key on the control VM, and the live secret files `instance/secrets/network.sops.yml`
+ `proxmox.sops.yml` (SOPS/age-encrypted, committed).
**Proves:** `scripts/lib/plaintext-guard.sh` stands in front of the commits that take a
whole worktree into the canonical with nobody reading the diff — the deploy's
instance-state commit (`local-canonical.yml`) and the launch-kit launcher's capture
(`scripts/kontroll.sh`) — and refuses, naming the file: a secrets file in which a value
does not have the shape of ciphertext (not merely "has a `sops:` block": a key added by
hand on a line of its own is refused with its line number — before the block, inside it,
and anywhere after its last line, which is where an editor puts a line appended at the end
of the file), any other file in a secrets directory (a decrypted copy, an editor backup),
and any file that holds a private key: an age identity anywhere in a line, a PEM or PGP
private-key header that begins its line, or such a header anywhere in a line when key
material follows it directly (after one literal `\n` in a quoted string, or on the next
line of a block scalar). It prints paths, rules and line numbers, never content. When it cannot look —
git cannot read the directory as a worktree, the directory is not the top of its
repository, the list of files does not reach the checker whole — it refuses, and both
callers treat every non-zero status as a refusal. `tests/validate` runs the same guard
over the secrets directories (step `sops-encrypted`; it needs no sops binary and no git)
and, where sops and pytest are installed, checks the predicate against documents the
installed sops wrote (step `sops-guard-conformance`, which fails when one of its tests was
skipped). `.gitignore` keeps the age key and the common decrypted-copy, key and backup
names out of the index — quietly, and only by name. Covering tests:
`tests/unit/test_plaintext_guard.py` (run under every awk implementation installed, since
the launcher's seat runs on the host's; it also keeps "the commits that take a whole
worktree" a true list: a blanket add under `scripts/`, `ansible/`, `docker/`, `api/` or
`gui/` that is neither gated nor explained in place fails it),
`tests/unit/test_launcher_capture.py`, `tests/integration/test_plaintext_guard_sops.py`,
`tests/unit/test_gitignore_secret_shapes.py`, `tests/unit/test_local_canonical.py`,
`tests/unit/test_launch_kit.py`. **Not covered:** a plaintext file that is ALREADY committed
and unmodified is not re-read at deploy (only `tests/validate` sees it — refusing it there
would turn an old mistake into a deploy outage whose remedy is history surgery); a commit
made by hand and then mirrored, and the scoped commits of the API and the GUI, which do not
pass this gate; a secret in a KEY name (sops does not encrypt names); a value written by
hand in the shape of a ciphertext token (the guard reads shapes; it holds no key); inside
the `sops:` block, a line put among the recipient entries, the VALUE of one of sops's own
scalar keys (a line named like one placed before `version:`, or text typed onto such a
line — the last line included), and a second block; a secret in a `README.md` inside a
secrets directory, or in a free-named notes file elsewhere, unless it holds a private key;
an armored key INLINED in another file whose body does not follow its header directly
(every PGP private-key block, a passphrase-protected legacy PEM — each is recognised only
where its header begins a line); a PEM header with nothing key-like after it, a key stored
base64-encoded as a whole, an age key in lower case, a key file saved as UTF-16, a binary
key store.
**Break-glass — a second recipient from an instance's first secret, unless declined.** A
break-glass key is a second age recipient on every domain whose private half the operator
holds offline. kontroll never writes that private half to the VM or the repo, and a key made
on another machine (`--break-glass-recipient`, the preferred choice) never reaches the VM at
all ([docs/break-glass.md](docs/break-glass.md)).
The shipped `instance.example/.sops.yaml` keeps ONE placeholder per rule by design:
`kontroll-init --fresh` replaces it with this instance's control key (minted unless one is
at the key file's path; an instance never inherits another's recipients), then requires a
choice and applies it before the first secret is written — `--break-glass-recipient <age1…>`
(the public half of a key made on another machine; preferred), `--mint-break-glass` (minted
on the node, its private half shown once on the terminal; kontroll writes it to no file, and
the installer keeps no container log of `fresh-init`), or `--no-break-glass` (ONE recipient
per domain, for a scratch instance). The claim therefore has three states:
- An instance created by `--fresh` with either of the first two has a second recipient on
  every domain from its first secret.
- An instance created with `--no-break-glass`, or before `--fresh` asked, has ONE recipient
  per domain until [docs/break-glass.md](docs/break-glass.md) is run on it, which adds the
  recipient under both anchors and re-wraps the files (`sops updatekeys`). For such an
  instance **losing the control key IS catastrophic**: every device credential, API token
  and service password in it becomes permanently unreadable.
- In either case kontroll counts recipients and **cannot know whether anyone kept the
  private half**: a recipient whose private half was discarded is a count of two and a
  recovery of zero. Only the operator's own check proves it — decrypting every domain with
  the break-glass key alone (the one key in the file, its public half the break-glass
  recipient), off the box, where sops can find no other key (docs/break-glass.md, "Verify").

This row once said every secret had two recipients, which was false for every instance —
worse than documenting no control at all, because it invited the reasoning a reclaim
decision would rest on.
An off-box *copy of the private key* is a backup, **not a break-glass recipient**: it
shares the single point of failure rather than removing it.
Tracked as **R-KEY-1** under [Accepted risks](#accepted-risks--known-limitations). The
bootstrap play warns, per creation rule, when a rule in `instance/.sops.yaml` names fewer
than two distinct age recipients in a key group (it reads the rules, not the encrypted
files — a recipient added but not yet re-wrapped is counted)
(`tests/unit/test_bootstrap_break_glass_warning.py`); `tests/unit/test_break_glass_claim_is_honest.py`
runs `--fresh` for each choice and holds this paragraph to what it writes.

**Pending:** the `compute` (docker SSH), `dashboards`, and `semaphore` secret files.

Device passwords, API tokens, and service secrets are **never written to the
repo in plaintext**. They are encrypted with the control VM's age key (recipient
in `.sops.yaml`) and decrypted at runtime in memory via the `community.sops`
vars plugin. One file per domain (network / proxmox / dashboards / semaphore / acme).

**Key management:** the age private key lives only at
`~/.config/sops/age/keys.txt` (mode 600) on the control VM, is **never
committed**. It stops being the single point of total loss only where a break-glass
recipient was added, every domain file re-wrapped to it, and its private half kept and
proven (above). This is the analog of NetConfig's
OS-keyring Fernet key, with a different mechanism (documented choice, PLAN.md §7).

### C2 — Secrets never logged  ⬚ planned

**Implements:** `no_log: true` on every Ansible task handling a credential or
decrypted value; `ansible.cfg` `log_path`.
**Proves:** a grep gate in `tests/validate` flags secret-handling modules
(`*_command`, `uri`, `*_token` vars) lacking `no_log`. Mirrors NetConfig's
`test_logging_config.py` "credentials are never logged" guarantee.

Scheduled jobs never run at `-vvvv` (can echo secrets despite `no_log`).

### C3 — Least-privilege network reachability  ⬚ planned

**Implements:** edge-firewall rules allowing the control VM (mgmt VLAN)
→ specific service ports on the server VLAN and the AP on the AP VLAN. **Scoped, not
any/any.**
**Proves:** documented in `docs/bootstrap-control-vm.md` §7; verified by an L3
smoke probe (only intended ports reachable). The compose layer enforces it too:
every published port binds `${KONTROLL_MGMT_IP}` (fail-closed `:?`, rendered from
`instance/instance.yml`), checked by `tests/check-mgmt-port-binding.py` in
`validate` — never `0.0.0.0` (the NPM-fronted homepage/Semaphore UIs excepted).

The control plane is **never bound to WAN** and is published internally only,
behind NPM at `control.${KONTROLL_DOMAIN}`.

The **homepage `:3000` all-interfaces bind is the documented NPM-fronted exception**:
Homepage is an *unauthenticated, read-only, no-actuation* tile board carrying no
secret in its served config — the rationale + the "keep `:3000` on the mgmt VLAN"
caveat live in [instance.example/dashboards/homepage/README.md](instance.example/dashboards/homepage/README.md).
The boundary is enforced at the NPM/firewall layer, never the container bind. The container receives **its own
env file** (`docker/.env.homepage`: the `homepage_var_*` keys of the `dashboards` domain, nothing else) — never the
stack `.env`, which carries every other secret and which it `env_file`d wholesale until 2026-10-09 (the 2026-10-08
review, finding 5; `tests/unit/test_homepage_env_isolation.py`). No new
control row — no trust boundary moves and the served YAML stays secret-free.

### C4 — Blast-radius / access-chain gating on changes  ◑ partial

**Implements:** access-chain header on every state-changing play (CLAUDE.md);
`--check --diff` before live; `risky` tag on path-severing plays.
**Proves:** review checklist + the header convention; per-class console fallbacks
(MAC-Winbox, HDMI console) documented in each role README.

Defence in depth against locking yourself out of the very device carrying your
session — the runbook discipline, applied to automation.

### C5 — Graceful degradation (fail soft on load, fail closed on secrets)  ✔ implemented (pattern)

**Implements:** `ignore_unreachable: true` on fleet plays (`playbooks/*.yml`);
a missing/invalid secret aborts the run.
**Proves:** `backup-configs.yml`'s `ignore_unreachable` tolerates a down host and
`ping.yml`'s TCP-probe block/rescue reports it honestly (no false "ok"); SOPS
decryption failure raises, never silently continues with an empty secret.

Same availability/safety split NetConfig uses: one bad *definition* warns and
skips; a bad *secret* stops the world.

### C6 — Repo hygiene  ✔ implemented

**Implements:** `.gitignore` (keys, `.env`, decrypted outputs, runtime data),
`.gitattributes` (LF); **`gitleaks`** deep secret scan in `tests/validate` + a
`pre-commit` hook (`.gitleaks.toml` allowlists SOPS ciphertext + the gitignored
`local/`; a custom rule always catches an `AGE-SECRET-KEY`). **`zizmor`** statically
audits the GitHub Actions workflows for supply-chain anti-patterns
(`.github/workflows/zizmor.yml`, advisory — a finding is logged, and uploaded to code scanning where the
repository variable `KONTROLL_CODE_SCANNING` is set, never blocking; the hybrid action-pinning policy
`.github/zizmor.yml` FLAGS an un-pinned third-party action, and the `docker/*` actions in
`publish-images.yml` are SHA-pinned, kept so by `tests/unit/test_ci_workflows.py`); every CI checkout sets
`persist-credentials: false`.
**Proves:** `tests/validate` fails on any plaintext key/credential in a committable
file (verified: a planted age key is caught); `git status` clean of secret artifacts.

---

### C7 — Git access is least-privilege, per-repo  ✔ implemented

The control VM's outbound git access is scoped so **no remote repo can be written
except where explicitly intended, and a leaked credential reaches only one repo.**
Every GitHub credential is an SSH **deploy key** (cryptographically bound to a single
repository — not an account-scoped token), pinned with `IdentitiesOnly yes` so each
remote offers only its own key.

| Remote (VM working tree) | Repo | Key | Access |
|---|---|---|---|
| `origin` | `netcanon/kontroll` (product code) | `~/.ssh/kontroll_deploy` | **read-only** (cannot write) |
| `local` | `/srv/kontroll.git` (the instance canonical) | n/a (local file) | the source of truth |
| `backup` | `netcanon/kontroll-prod-test` (optional offsite DR) | `~/.ssh/kontroll_backup_deploy` | **write, this repo ONLY** |

**Implements:** per-repo deploy keys + `~/.ssh/config` host aliases with
`IdentitiesOnly yes`; private keys are generated on the VM and never leave it.
**Proves (verified):** `ssh -T git@github-backup` → "Hi netcanon/kontroll-prod-test!"
(the backup key authenticates only as that repo); the same key against
`git@github.com:netcanon/kontroll` → **denied** ("Repository not found"). The instance
operates entirely from the `local` canonical, so even read-access to `origin` is
optional (developer code-sync only — `scripts/update.sh`). Offsite backup is opt-in
(`scripts/backup.sh`); the encrypted secrets are safe to host, the age keys never leave.

---

### C8 — Local state at rest is the trust boundary  ◑ partial

Since the *instance* (not GitHub) is the source of truth, the local state **is** the
crown jewel — protect it where it lives, not at a remote.

**Implements:**
- Secrets are **SOPS-encrypted** wherever state flows — the canonical repo, the offsite
  backup, the DR snapshot, and the install bundle all carry only ciphertext (C1). For the
  canonical, the commits that take a whole worktree unread are gated by
  `scripts/lib/plaintext-guard.sh`; a commit made by hand and then mirrored, and the scoped
  commits of the API and the GUI, are not re-read (C1, "Not covered"). The
  control key is on the VM — and, where a break-glass recipient was added, the break-glass
  key's private half is offline with the operator — never in any of those artifacts.
- The local canonical (`/srv/kontroll.git`) is operator-owned; the runner mounts it
  **read-only** unless the privileged tier is armed (C10: `api_privileged` renders the mount `:rw`, which is what
  lets the `promote-proposal` task fast-forward `main`); the secret captures (`/var/lib/kontroll/backups`) are
  `0700`/uid-1001. The deploy makes the dir `0700`, and so does every `file` task under `ansible/` that makes it —
  the backup play's, and those of the class `backup` entrypoints that make it themselves, which until 2026-10-22
  made it `0750` at every backup (`tests/unit/test_backup_dir_mode.py`).
- **Retention pruning stays inside this boundary (#125).** The capture-history prune (`backup-configs.yml`)
  rewrites the commit graph of the **same** `0700`/uid-1001 captures dir to enforce a finite
  `kontroll_backup_retention` window. It is **fail-closed**: it runs only when a finite window is set
  (`keep-all`/undefined ⇒ no-op) and refuses unless the target is a **non-bare git worktree at
  `config_backup_dir` with NO git remote** that is **not** `/srv/kontroll.git` and has no root `CLAUDE.md`. The
  **load-bearing distinguisher is the no-remote check** — the captures repo never has a remote; the canonical AND
  the deploy tree both do (the deploy tree is caught by this leg, since a clone always has a remote). The
  **canonical** is additionally excluded several independent ways (bare-repo check, `rev-parse` rc, `.git` stat,
  path regex). So a wrong-repo history rewrite of the source of truth is designed out — verified by an adversarial
  review that ran the prune against a symlinked canonical, the bare canonical, and the deploy tree and could not
  reach a rewrite. It touches **commit metadata/trees only, never prints capture content** (no secret logged), and
  the reclaimed history is **local-only with no remote/offsite copy** — pruning it moves **no trust boundary** and
  the C10 canonical's `denyNonFastForwards` posture is untouched. **Accepted residual:** a per-class snapshot
  commit from a *concurrent* backup run could be orphaned by the rewrite and reclaimed — bounded to one historical
  diff point on local-only ephemeral data (never the canonical, never the current capture, which the next run
  re-captures), serialized prune-vs-prune by an mkdir lock and mitigated by the staggered crons. Pinned by
  `tests/unit/test_backup_prune.py` (the fail-closed guard + `--check` safety + behavioral tmp-repo tests covering
  pre-cutoff drop, idempotency, all-older, and crash-recovery stale-ref cleanup).
- The at-rest stores are **relocatable** (`KONTROLL_STORAGE_ROOT` + per-class overrides — put logs/backups on a
  data disk) but the **uid/0750 chown FOLLOWS the path**: one `.env` var feeds BOTH the compose bind source AND
  the deploy-stack chown task, which runs **before** `up` — so a relocated store is created operator-/uid-scoped,
  never a Docker-auto-created `root:root 0755` (world-readable) bind. Enforced fail-closed by
  **`tests/check-storage-chown.py`** (a bind source with no uid-scoped provisioner fails CI — the G9 guard); the
  same change **closed two pre-existing holes** (the onboard-gui `age.key` gained a fail-closed absence guard, the
  `caddy/` dir gained an owner/group). An empty/`/` root is rejected by a deploy-stack assert.
- `scripts/make-bundle.sh` strips the **entire `instance/` overlay** (recipients, all `*.sops.yml`,
  inventory IPs, fleet, dashboards) + the internal `docs/reviews/` snapshots, shipping only the
  `instance.example/` stub; `scripts/state-snapshot.sh` excludes the age key by design — a leaked
  bundle/snapshot **decrypts nothing** and names no recipient. (Pinned by `tests/integration/test_make_bundle.py`.)
- The state boundary is explicit + machine-checked-able (`config/state-manifest.yml`),
  so backup/DR cover exactly the state and nothing leaks code/secrets unintentionally.

The **onboarding GUI is privileged** (writes the working tree, encrypts secrets, runs
Ansible) and is now guarded: **HTTP Basic auth, fail-closed** (refuses to start without
`GUI_PASSWORD`, constant-time compare, password from SOPS), **TLS** (`GUI_TLS_CERT/KEY`),
and an **audit log** of every action + auth-denial (no creds). Verified: no-auth → 401,
HTTP→HTTPS-port refused, wrong password → 401, denials logged.

**Residual (tracked):** the GUI uses a **self-signed** cert + no SSO/rate-limiting — fine
mgmt-only, but front it with a CA cert + SSO before any wider exposure. Inventory IPs
travel **plaintext** in backups/snapshots — keep those artifacts on trusted media.

---

### C9 — The onboarding API is a privileged surface  ◑ partial

The typed API (`api/`) is the second privileged surface: it can rebuild the capability
cache, declare capture-exceptions, **commit + push the local canonical**, run the full
**onboard** mutation (module + drop-in host + fleet-enable + creds→SOPS, dry-run by default), and —
since Capability-track Phase 7 — the **secondary-capability promote** (`POST /capability/{cap}`,
audited `capability-promote`): a propose-then-promote, plan-hash-gated, data-only write of a capability
block (telemetry today) committed to **only the plan's scoped paths**, carrying **no credential** (the
chosen exporter's creds stay SOPS; the audit logs the secret-domain NAME only). The whole
`/capability/<cap>` prefix is privileged — a registry-parametrized tripwire (`test_api_ratelimit`) keeps
any future capability route inside the token budget. It is guarded like the GUI (C8), extended for a
token-auth service. An onboard of a collection that carries no signal a backend classifies on is refused — 422,
audited `onboard-refuse reason=no-backend` with nothing from the collection — and never routed to SSH: `raw_ssh`
(SSH as root by default) is chosen only by a class's `backend:` declaration or its Generic SSH host search card
(S24; `tests/unit/test_classify.py`, `test_search_card.py`, `test_api_onboard.py`). A search card may name only a
collection ansible-core ships (`paths.ANSIBLE_CORE_BUNDLED`): the planner neither probes nor pins a card's
collection, so a card naming a Galaxy collection would onboard it unpinned (`test_search_card.py`, the
`galaxy-collection` row).

**Self-describing onboard credentials (F1) — domain confinement.** The onboard form's credential fields are
DERIVED per-backend from each backend's declared `auth:` shape (`scripts/kontroll/authspec.py` — a `network_cli`
device gets SSH login, a REST device a token), replacing the static 4-field union with one data-driven loop. The
field DESCRIPTORS are pure schema (names/kinds, **never values** — the same no-leak posture as the rest of the
onboard plan: values transit `creds_to_set` in memory → the stdin-only SOPS write, MF-3). Because the mapping is now
data-driven, a **domain-confinement guard** (`build_onboard_plan`, MF-S2) refuses — loudly, catchably, BEFORE any
`creds_to_set` write — any derived SECRET field stamped for a SOPS domain other than the one the onboard targets, so
a malformed/adversarial descriptor can never smuggle a secret into a domain the operator didn't choose (the hardcoded
if-ladder couldn't cross domains; a data-driven loop could, so the invariant is now enforced in code, not by
convention). Pinned by `test_onboard_ssh_key.py::test_domain_confinement_refuses_a_cross_domain_secret`. The GUI
half (Rung 0a-2) renders these derived fields from a new READ-ONLY route (`GET /onboard/cred-fields`, mirrored on
the typed API + the in-process GUI) and from the dry-run plan's `cred_fields`; both surfaces project every
descriptor through `authspec.public_descriptor`, a closed-key whitelist that drops any value-shaped key, so the
field SHAPE on the wire stays **names-only** even if a descriptor ever grew a value (belt-and-braces, not just
convention). The read route is auth-gated under the privileged onboard prefix and writes nothing (never gates
onboarding, INVARIANT D*). Pinned by the no-value-on-the-wire asserts in `test_api_onboard.py`/`test_gui_api.py` +
`test_authspec.py::test_public_descriptor_projects_only_schema_keys_drops_anything_else`.

*Rung 1 — coherent `auth_set` + `shared` domain creds (seam S1).* A curated class may declare its own `auth:` block
(a multi-field `auth_set`, e.g. proxmox's API token user/id/secret) that wins on the reuse path. A field marked
`shared: true` is a DOMAIN-level credential — stored FLAT under its `sops_stem` (not host-keyed), so one read-only
service token serves the whole domain (the pve-exporter's `secret_env_map`). This is **still domain-confined**: the
field's `domain` is stamped from the class's `secrets_domain` and the SAME MF-S2 guard refuses any cross-domain
secret before a write; `shared` only changes the KEY shape (flat vs `<host>_`-prefixed) within the chosen domain,
never which domain. The planner additionally refuses a **partially-filled** required `auth_set` (a half credential is
catchable-rejected, never silently stored) while a values-free dry-run still plans. No `ansible-doc` introspection is
added (config-as-data only; the argspec tier is Rung 3). Pinned by `test_onboard_proxmox_auth.py` (flat-keying +
coherence + dry-run) and `test_authspec.py::test_proxmox_shared_sops_stems_match_the_pve_exporter_secret_env_map`.

*Rung 3 — TIER-B argument_spec introspection (`deep`), with two hardening gates.* When `deep`, `authspec.
auth_fields_from_argspec` reads the connecting module's `argument_spec` via `ansible-doc -j` and derives the EXACT
credential fields (proxmox's coherent `api_user`+`api_token_id`+`api_token_secret`, not a generic single-token box).
Two adversary-driven gates (design record: `docs/reviews/2026-06-29-north-star-onboard/99-synthesis.md`):
- **MF-S1 — install-confinement, FAIL-CLOSED.** `ansible-doc -j` imports the collection's Python (code execution).
  This is **not a new exposure** — `deep_probe` already runs `ansible-doc -j` on every deep search, and the trust
  boundary is the **install** (already gated by the never-brick L2b recorded-sha256 floor, C15-a). TIER-B introspects
  only the SAME already-installed collections. The `coll in local_installed()` check is a fail-closed invariant
  **inside `auth_fields_from_argspec`, before any `ansible-doc` call**, so no future caller (e.g. a "sharpen a
  Galaxy result" path) can bypass it into executing an **un-installed** collection's Python. TIER-B is never reached
  for a Galaxy result — an un-installed collection stays on the coarse TIER-A shape at zero code-execution cost.
  Pinned by `test_authspec.py::test_tier_b_install_confined_never_introspects_an_uninstalled_collection_mf_s1`
  (asserts the `ad` seam is **never called** when the collection is absent).
- **MF-S6 — mask-ambiguous, FAIL-SAFE.** `ansible-doc`'s `no_log` is not a complete secret oracle (a module can set
  `no_log` only in code). A genuinely-ambiguous derived field (no `no_log`, no secret-class name, but a required
  member of an auth-bearing module) defaults to **masked/secret**, never plaintext — masking a non-secret is
  harmless; plaintexting a missed secret is a leak. Pinned by
  `test_authspec.py::test_tier_b_masks_a_genuinely_ambiguous_required_field_mf_s6`.

TIER-B changes only WHICH fields are offered — the descriptors stay pure schema (the same `public_descriptor`
names-only wire projection), MF-S2 domain-confinement still stamps + checks every field's `domain`, and any miss
(un-installed / flaky `ansible-doc` / ambiguous) degrades to the TIER-A shape, never a blocked onboard (INVARIANT
D*). No new value-bearing surface, no new install path, no new network call.

**Self-describing capability FLOOR (F2 de-bespoke) — creds-free auto-derivation (MF-S5).** A blind onboard now
AUTO-DERIVES the universal monitoring/logging floor (snmp + blackbox metrics, syslog logs) from each backend's
`confers.telemetry`/`confers.logs`, written into the new module (`classify.derive_class_capabilities`). The
security invariant: **a method is auto-derivable only if it needs no PER-CLASS secret** — its `secret_domain` is
null/absent, OR it reuses one of the closed `classify.SHARED_DERIVABLE_SECRET_DOMAINS` (today just
`snmp_observability` — the SNMPv3 USM creds provisioned ONCE for the whole fleet, control + break-glass, READ-ONLY
GET, never actuation). A method with a per-class secret (`pve`→proxmox, `rest_pull`→its own token) is NEVER in a
confer list, so it can never be silently auto-attached to a blind device with an unfilled credential (the never-fake
invariant, enforced in `_is_creds_free_to_derive`, not just by convention). The derivation is PURE/offline (no
network at generate time, BRICK-1) and best-effort (a bug degrades to no floor, never a failed onboard). Pinned by
`test_derive_class_capabilities.py` (`test_every_conferred_method_is_creds_free_to_derive` +
`test_vendor_narrow_credentialed_method_is_never_faked_but_named_in_the_gap`).

**Derived dashboard FLOOR (Rung 4a) — even the board id is derived, and the derive tier is out-of-band.** A telemetry
method carries a `derive_dashboard:` SELECTOR (a search query + datasource + a required series, NOT an id);
`scripts/gen-dashboard-floor.py --resolve` derives the grafana.com board from the parseable public registry and PINS
`{id, revision, content_sha256}` into `dashboards/derived/<method>.lock.yml`. Unlike the metrics/logs floor (a
pure-offline derivation), a board id is derived over the network — so the security posture is the **BRICK-1 split**,
not offline-purity: **(a)** `--resolve` is an OPERATOR / control-VM tier — egress-only to grafana.com, **no
credential** (community boards are public), never run in CI/deploy (a `validate.sh` static gate greps it out of
`deploy-stack.yml` + the workflows; pinned by `test_resolve_is_absent_from_the_install_gating_path`); **(b)** the
install-gating leg is `--check`, **hermetic** — it re-hashes the committed board against the lock's `content_sha256`
(the offline tamper floor) + re-greps the pinned board for its `requires_series` (the series-fit honesty guard) with
**no network** (pinned by `test_check_is_offline`). A board is emitted only if it queries the method's series (the
never-fake invariant, `resolve_method`'s series-fit fall-through), and a board SWAP is loud (MF-5). The pinned board
JSON carries no id (`pin_datasource` nulls it) and no instance data (a public dashboard); P-1 clean. **Wiring (Rung
4a-2):** a `derived: true` class's `dashboards:` block is emitted from these locks by `classify._derive_dashboards`
and owned by `gen-class-capabilities --check` (a 3rd drift-gated capability). **INVARIANT-D:** the onboard path
writes the offline creds-free metrics/logs floor but **NOT** a `dashboards:` block (a board rides the out-of-band
locks, not the onboard write surface) — its write blast-radius is unchanged; a blind onboard's board is re-derived by
the operator, and the honest `capability_gap` names an unresolved selector.

**Implements:**
- **Bearer-token auth, fail-closed.** Privileged routes require `KONTROLL_API_TOKEN`
  (SOPS-backed at deploy, like `GUI_PASSWORD`); **unset ⇒ the privileged routes are disabled
  (503)**, never open. The token is compared **constant-time** (`hmac.compare_digest`); a
  bad/absent token is a 401. Read-only inquiry (search/probe/units/classify/health) needs no token.
- **A collection path param is resolved against the installed set or checked against `namespace.name` before
  it is used (SEC-3), never joined raw.** The
  collection-keyed read routes (`/probe`, `/units`, `/classify`) take a `{collection}` from the
  caller. `service/units.py` matches it against the names `ansible-galaxy collection list` reports and
  only then walks that collection's own on-disk dir — so a `../`/absent/crafted value is a clean 404,
  never a filesystem traversal; verified by `tests/unit/test_units.py` (the walker is made to explode if
  a non-member name ever reaches it). `/probe` and `/classify` hand the name to `ansible-doc`, so
  `probe.deep_probe` first checks that it is exactly `namespace.name` (C22): an option-shaped or otherwise
  malformed value is a 422 and no process is started.
- **The configure form (R3) is a write-free, fail-closed read surface.** An actuation unit's CURATED
  knobs render through the one knob renderer (`configurable` kind `actuation-unit`); their submitted
  values re-validate server-side via `POST /units/{key}/configure` → `service/units.py::validate_unit_config`,
  which **stages nothing** (pinned `assert_read_only`; the #134 completeness gate sees no new write verb).
  It is FAIL-CLOSED on a CLOSED allow-list (a var the descriptor doesn't declare is rejected) + the P0a
  `_validate` guard per knob — and a **secret-typed actuation var is FORBIDDEN at the descriptor layer**
  (`gen-actuation.validate_knobs`): there is no per-unit SOPS domain, so a unit may not carry a credential
  (SEC-2 — secrets route through the Secrets dialog). No configure value is ever a secret; no value is logged.
  The **Review-stage preview** (R4, `POST /units/preview` → `service/actuation.py::build_actuation_plan`) is the
  same write-free, fail-closed read: it re-validates the values, renders the would-run play (the role wrapper /
  FQCN run-spec + the derived access-chain header), and **stages nothing** (pinned `assert_read_only`; no new
  write verb). The first write verb — staging `proposed/<run_id>` + the signed install — is the R5 rung.
- **Audited, fail-closed.** Every privileged call writes an append-only, size-rotated TSV line —
  `ts, user, ip, action, run_id, detail`, **never a credential** — and the write is **fail-closed**:
  a mutation that cannot be audited is *refused* (503), not performed. Denied auth attempts are
  logged. A privileged `GET /audit/log` makes the log queryable.
- **run_id correlation.** Each privileged request mints a `kontroll_run_id` at the boundary,
  carried in the audit line (and, when a route triggers Ansible, passed down) — one operation is
  greppable end-to-end (docs/logging-architecture.md §3).
- **Config-injection guard — a typed, fail-closed server-side validator (P0a, #133).** What makes the capability
  write "data-only" is that every knob value is re-validated **server-side** against its descriptor before it can
  reach a rendered `module.yml` block — the GUI widget is convenience; the descriptor is the contract; the server
  is the guard. `service/_validate.validate_value` is the per-knob-`type` registry behind the shared
  `_blockwrite.validate_params`: `enum` (a CLOSED allow-list — the original guard), `bool`, `int`
  (`range`-bounded), `ipv4` (`ipaddress`), `hostname` (RFC-1123 labels), and `text` (a **required** `pattern`,
  `re.fullmatch`'d so a trailing metacharacter/newline can't ride past). It is **fail-closed**: an unknown `type`,
  or a `text` knob with no `pattern`, is rejected — so widening the GUI's widget vocabulary (Phase 3) can never
  weaken the closed-allow-list guarantee. This is the runtime, degrade-not-crash twin of `gen-observability`'s
  generate-time fail-closed `_validate_params` (C12) — defense in depth. Covering checks:
  `tests/unit/test_validate.py` (each type with its injection vector) + `test_blockwrite.py`. The widened GUI
  widgets that ride this guard are rendered from the **read-only** `service/configurable.configurable_view`
  projection (`GET /api/configurable`, Phase 3, #137) — it stages/decrypts nothing (pinned `assert_read_only`)
  and a secret knob it projects is NAMES-only, write-only, **never pre-filled** (C11); the write path is unchanged
  (`/api/capability`, `/api/secrets`).
- **Safe reconfigure — the GUI can now stage a CHANGE to a declared capability block (Phase 4a, #138).** A
  declared telemetry/backup/logging block is no longer a wall; the dialog reads the current values, computes a
  field-level diff, and can `upsert_into_block` (a comment-preserving line-span replace). The **anti-clobber**
  guarantee rests on THREE controls, of which the anti-drift token is **NOT one** (M1): (1) `verify_no_drop`
  (`service/_reconfig.py`, generalized from `keygen._verify_additive`) — the spine refuses at promote
  (`would_drop` 409) if the write would silently drop a co-owner's declared method/param, fail-closed; (2) the
  **human severity-confirm** — Promote is gated until the operator acknowledges a `modify`/`remove`; (3) C10's
  **two-key promote** (the GUI only stages `proposed/<run_id>`, Semaphore promotes), unchanged. The `plan_token`
  is **anti-drift only** — it now folds the severity decision (`token_parts += "<severity>|<sorted
  will_overwrite>"`) so a recomputed-more-destructive promote yields a different token (no-quiet-upgrade), but a
  token is not authorization. Every upserted value still passes the closed-allow-list `validate_params` (P0a). No
  new write-to-`main` hole (no `promote_ref` import; `_push_target` fail-closed). (Secret-field rotation — the
  NAMES-only `will_overwrite` + confirm M-R-B prerequisite this note named — now ships under **Secret rotation
  (#141)** below.) Covering checks: `tests/unit/test_reconfig.py` (no-drop fail-closed), `test_diff.py`,
  `tests/integration/test_gui_api.py` (drift-with-diff 409), `tests/e2e/test_reconfigure_flow.py` (the confirm gate).
- **Secret rotation — the Secrets dialog can now rotate a service login (PR-1/PR-2, #141).** Reusing the existing
  mint+stage path (`secrets.build_secret_plan`/`apply_secret_plan` → SOPS ciphertext, C10 `proposed/<run_id>`),
  a `rotatable: true` field in `secret-forms/<domain>.yml` may be re-saved with a new value. The anti-clobber
  guard is the **M-R-B prerequisite C9 named**: `secrets.overwrite_set` re-derives, SERVER-side, the NAMES of
  already-set fields a save would overwrite (never client-trusted, M1), and the route/GUI **fail CLOSED**
  (HTTP 409 / `secret-overwrite-confirm`) until the caller acks `overwrite:true`. The confirm + the audit carry
  field NAMES only — never a value, never a before→after (C11/C12); the `secret-apply` audit gains an
  `overwrite=` marker. Actuation does **NOT** move toward the GUI: a new value is staged, and making it LIVE is a
  **post-promote hand-off** — `secrets.actuation_enact_commands` emits command STRINGS (`deploy-stack` recreate /
  `grafana-cli` reset / `semaphore-admin` = recreate **then** an env-ferried `semaphore users change-by-login`)
  the operator/Semaphore runs; the GUI/API execute nothing and mount no docker socket (the C9/C10 trust boundary
  is unchanged — accepted-risk clarification). The `semaphore-admin` kind (PR-2) exists because Semaphore applies
  `SEMAPHORE_ADMIN_PASSWORD` only at first-run setup — recreate-alone cannot rotate an existing admin (verified
  live on v2.18.12); the CLI step reads login+password from the **container's own env** (set by the recreate), so
  the value never reaches the host argv or shell history. The only residual is the value on the in-container argv
  of one short, operator-run command (accepted; bounded to the container's own PID namespace, no stdin path in
  the upstream CLI). A per-FIELD `rotatable` allow-list keeps rotation TRAPS off the surface
  (`semaphore_db_password`, `semaphore_access_key_encryption` — rotating either alone breaks the running
  service). Covering checks: `tests/unit/test_secrets_service.py` (overwrite_set NAMES-only + server-derived;
  enact carries no value; non-rotatable & rotatable-without-actuation emit nothing; the `semaphore-admin`
  two-step ferries by env NAME and embeds no value), `tests/integration/test_api_secrets.py` + `test_gui_api.py`
  (409 fail-closed then ack; `overwrite=` audit marker with value absent; enact present + value-free — API/GUI
  parity), `tests/e2e/test_secret_rotation_flow.py` (the `secret-overwrite-confirm` gate + the `secret-enact`
  hand-off render).
- **Heavier reconfigure — module identity + inventory host-var (Phase 4b, #139).** The GUI can now stage a
  change to a class's **identity** (`secrets_domain`/`inventory_group`/`backend`/`role` in
  `modules/<key>/module.yml`, via `service/identity.py` + `_blockwrite.upsert_top_level_key`) and to a host's
  **connection vars** (`ansible_host` in the banner-owned inventory drop-in, via `service/hostvars.py` +
  `gitio.owned_merge`). Higher blast — an identity re-point **re-homes hosts / orphans creds** — so the SAME
  three anti-clobber controls apply, with two reinforcements: (a) the per-knob `severity` from the
  `settings/<area>.yml` descriptor **upgrades** an identity change to a **type-to-confirm** gate
  (`reconfig-identity-confirm` — the operator must retype the new value), re-derived server-side and folded into
  the token's severity decision (M1, no-quiet-upgrade); (b) a proposal may change **at most one** identity-severity
  key (so the type-to-confirm is unambiguous). `verify_no_drop` is now **load-bearing** (host-var/identity edits
  produce real co-owner data on the same file); `owned_merge` lifts `write_inventory_host`'s silent `.update()`
  into a reported collision. Every value still passes the closed-allow-list validator (P0a — `ipv4` for
  `ansible_host`; a slug pattern for the identity keys). **Secrets stay out:** the host's templated cred-lookup
  vars (`{{ lookup(…) }}`) are **read-only** (rotating a credential is the secret path — M-R-B, not this surface);
  no secret value is read, returned, or written. No new write-to-`main` hole (the shared `_reconfig.run_promote`
  stages `proposed/<run_id>`; no `promote_ref` import; `_push_target` fail-closed). New write verbs
  (`owned_merge`, `run_promote`) joined `WRITE_VERBS`; the new reads are pinned read-only
  (`tests/unit/test_reconfig.py`). Covering checks: `test_identity.py` (single-identity rule + crafted-value
  reject), `test_hostvars.py` (the cred-lookup stays read-only + `owned_merge` collisions), `test_gui_api.py`
  (both routes stage + server-derived-severity audit + stale-token 409), `test_reconfigure_flow.py` (the
  type-to-confirm gate).
- **Platform Settings EDIT — the GUI can now stage a tracked instance.yml / fleet.yml change (Phase 5, #140).**
  The highest-blast GUI write: `mgmt_ip` (a re-IP that severs this very `:8443` listener), `domain`/`tls_mode`
  (a re-render / plaintext flip), and `enabled_modules` removal (drops monitoring for live hosts). All ride the
  SAME three anti-clobber controls + the descriptor-driven severity gate — `mgmt_ip` is `severity: identity`
  (**type-to-confirm** the new IP, re-derived server-side), `domain`/`tls_mode` `redeploy`, fleet removal
  `remove`; each carries a consequence-naming `confirm_text`. `verify_no_drop` runs over the **full** instance.yml
  (a dropped co-owner key fails closed). Every value passes the P0a validator (`ipv4`/`hostname`/`enum`) before
  the comment-preserving write. **FORBID (the load-bearing boundary):** `api_privileged` (the C10 arming switch —
  a settings page that could disarm itself is a footgun), `docker/.env` (the rendered output holding decrypted
  secrets — C11/C12), and `instance/.sops.yaml` recipiency have **no** edit route — a knob not in the
  `settings/<area>.yml` descriptor set is a 404; the status group surfaces arming **read-only**. No secret value
  is read, returned, or written; the page edits the INPUTS, never the `.env`. The write STAGES `proposed/<run_id>`
  (no `promote_ref` import; `_push_target` fail-closed); `disable_in_fleet`/`enable_in_fleet` RAISE (never
  `sys.exit` — the in-process worker survives, S-3/G9). Covering checks: `test_settings.py` (the danger knobs +
  full-struct no-drop + the FORBID/unknown-knob boundary), `test_gui_api.py` (C10 staging + FORBID 404 +
  stale-token 409), `test_settings_edit_flow.py` (the entry controls).
- **Rate-limited, fail-open-on-malfunction.** A single in-process middleware (`api/ratelimit.py`) caps
  request rate per class — **per-IP** for the open inquiry routes, **per-token-digest** (never the raw
  token) for the privileged routes; a breach is a **429 enforced *before* auth runs** (so a flood can't
  force probe I/O or spam the audit) with a best-effort, throttled `rate-limited` audit line. It **fails
  open only on its own internal malfunction** — auth + audit stay fully fail-closed, so no path admits an
  unauthenticated or unaudited mutation. Limits are a deploy-time `KONTROLL_API_RATELIMIT` knob (not a
  secret); covering check: `tests/integration/test_api_ratelimit.py`.
- **Read-only without `api_privileged` / never WAN; frontend exposure is a per-instance mode.** A compose service
  (`docker/services/api.yaml`) serves on the mgmt VLAN (`<mgmt-ip>:8444`); without `api_privileged` its content
  clone is root-owned and **no token** is set — so only the inquiry routes serve and the privileged routes are
  fail-closed (503); C10 is what arming adds. The
  TLS posture is **config-as-data**: `instance/instance.yml` `frontend.tls_mode` (resolver
  `scripts/kontroll/frontend.py`, covering check `tests/unit/test_frontend.py`) selects `byo_proxy` (HTTP
  behind the operator's reverse proxy, which owns TLS — this instance: Nginx Proxy Manager), `self_signed`
  (uvicorn TLS from a host cert — the zero-config default), or `acme` (planned, Caddy/Let's Encrypt); an
  unknown/empty mode fails **SAFE** to self_signed. Enabling the privileged mutations (token + a writable
  canonical clone + the age key) is a deliberate, flag-gated least-privilege step — its boundary + posture (propose-then-promote, **no** age key) is **C10** below ([decision record](docs/privileged-mutation-enablement.md)). mgmt-VLAN-only behind the
  GUI/Semaphore boundary (C3) — never WAN-exposed.
- **Metrics stack: mgmt-bound, read-only, no new secret.** Prometheus + Grafana publish on the **mgmt IP
  only** (`<mgmt-ip>:9090` / `:3002` — C3 parity with the API, never `0.0.0.0`; reached externally via
  NPM); `pve-exporter` publishes **no host port** (scraped only over the internal `kontroll` network). The
  exporter reuses the **existing read-only `*.Audit` Proxmox token** (`proxmox` SOPS domain, rendered
  `no_log` into `.env`) — a least-privilege *read* credential, never the Ansible mutate token; a leaked
  exporter token yields read-only cluster visibility, never actuation. Exporters read — they change nothing
  on the fleet (pull model: a scrape failure degrades a panel, not a device).
- **Host agent (node_exporter): additive, read-only, VLAN-scoped.** The `node_exporter` role installs
  `prometheus-node-exporter` (OS-aware: apt+systemd / apk+OpenRC) on `via: host` classes (hypervisors /
  docker_hosts); it exposes host
  internals (CPU/mem/disk/process) on `:9100` — **sensitive, not secret**. Reachable only on the mgmt/server
  VLANs the control node scrapes from; **never published to WAN** (C3). It carries no credential and actuates
  nothing (a metrics listener); install is idempotent + `--check`-first + reversible (`apt remove`). The
  scrape target is generated; the agent is the only fleet-side state this adds.
- **Agent-less SNMP (switch/firewall): read-only pull, scoped new secret.** `snmp_exporter` (no host port,
  `kontroll`-net-only) SNMP-walks the **Cisco core switch + FortiGate firewall** — the highest-blast-radius
  devices — over **SNMPv3 authPriv (SHA-1/AES-128)**, a **READ** credential: SNMP GET reads counters and
  **actuates nothing** (a creds/reachability failure blanks a panel, not the lab). The v3 creds live in a new
  **`snmp_observability` SOPS domain** — control + break-glass only via the `.sops.yaml` catch-all, **NOT** the
  scoped Semaphore key (a monitoring read-cred Semaphore never needs). They are rendered `no_log` by
  `deploy-stack` into `prometheus/exporters/snmp/snmp.yml` (mode 0600, a second on-disk secret location),
  **gitignored / never tracked** (a `tests/validate` check enforces it; only the `snmp.yml.j2` template is
  committed). `?target=`/`?module=`/`?auth=` are bounded to the **generated** inventory targets (no
  caller-supplied target ⇒ no SSRF); the per-class `module` param is a **closed allow-list** (rejected if
  unknown, never interpolated).
- **Agent-less reachability (blackbox_exporter): read-only probe, NO credential.** `blackbox-exporter` (no host
  port, `kontroll`-net-only, `cap_add: [NET_RAW]` for ICMP raw sockets only) ICMP/TCP/HTTP-probes the **switch +
  firewall** for reachability — a ping/connect, **no login and no actuation**, and **no secret at all**
  (`secret_domain: null`, unlike snmp). The `?target=` set is bounded to the **generated** inventory addresses
  (no caller-supplied target ⇒ no SSRF); the per-class `probe` param is a **closed allow-list**. It complements
  snmp — answering "is the device up" even when SNMP is silent, with zero device-side provisioning.

**Residual (tracked):** a **static** Bearer token (no per-user identity / rotation / SSO); the frontend
TLS posture is now a per-instance **mode** (`self_signed` default / `byo_proxy` / `acme` — see the deploy
bullet), so "self-signed" is a *chosen mode*, not a gap. Add SSO before any wider exposure (the same gate
as the GUI's C8 residual). Rate-limiting is now in place (above).
Verified: no token → 503, bad/missing token → 401 (+ audited), audit-unwritable → mutation 503,
over-limit → 429 (an audit-unwritable breach still 429, never 503).

### C10 — Privileged-mutation enablement (the network service writes the canonical)  ✓ live-verified (gated by `api_privileged`)

Turning on live actuation from the deployed API/GUI (the C9 surface) crosses one trust boundary beyond C9's
fail-closed token: an always-on, network-reachable service gains **write toward the local canonical**
`/srv/kontroll.git` (the source of truth every clone + deploy trusts, C8). Posture **decided** (threat model:
`docs/reviews/2026-06-13-roadmap/01-design-1b-privmut-security.md` rec #2 — a design record kept in the
private repository; decision record:
[docs/privileged-mutation-enablement.md](docs/privileged-mutation-enablement.md)): **propose-then-promote,
no key.**

**`trust_mode` — who turns the second key (F3, Rung 0c — flag only).** A per-instance `trust_mode`
(`instance/instance.yml`, [docs/trust-mode.md](docs/trust-mode.md)) declares how a staged proposal is promoted:
**`separated`** (a human runs the `promote-proposal` Semaphore task — prod, today's system) or **`solo`** (homelab —
declares intent to auto-promote). It is chosen explicitly at fresh-init and **never silently defaulted** — a trust
posture is the one setting kontroll refuses to guess (defended at three layers: `kontroll-init --fresh` refuses
without `--trust-mode`, the template ships it commented, and `deploy-stack.yml` hard-fails an unset/invalid value).
This release ships the **flag only**: the GUI/API trust boundary below is **byte-identical in both modes** (still
propose-only, still AST read-only-pinned; how the canonical is mounted is `api_privileged`'s to decide, never the
posture's), so the posture adds **zero new residual**. The
auto-promoter that gives `solo` its effect — and its honest residual (a GUI RCE → auto-promoted proposal) bounded by
a blast-tier refusal / rate / diff-size guard set — is a **separate, gated rung** that this edition does not ship
(it is the homelab (permissive) edition's control **C17** — see the note under C18); here `solo` behaves like
`separated`.

**Implements (gated by `api_privileged`; a deploy without it is read-only):**
- **The service only PROPOSES.** When it runs (`KONTROLL_STAGE_PUSHES=1`), `gitio.commit_and_push` pushes a
  **staging ref** `refs/heads/proposed/<run_id>`, never `main`; the operator's CLI (no such env) takes its
  commit to `main` directly (the trusted human) — as a **landing** through the promote gate, never a push (see
  "Every way into `main` lands" below). `kontroll-promote.py <run_id>@<commit_id>` fast-forwards `main` (FF-only;
  refuses otherwise). The in-GUI follow-on is now the **`promote-proposal` Semaphore task** (item C, 2026-06-15;
  `config/semaphore/templates/promote-proposal.yml` → `ansible/playbooks/promote.yml`) — a `run_id` survey var that
  takes `<run id>@<commit id>`, FF-only, **reachable only by a Semaphore admin on the mgmt VLAN, never the API token**
  (the promote lives outside the token's reach, so a leaked token can never self-promote). A leaked token can only
  **stage proposals**. Under `separated` each one waits for you to reject or promote it, so the token alone never
  rewrites the lab ([docs/trust-mode.md](docs/trust-mode.md) says what `solo` changes). **A proposal is
  all of its paths or none:** `git add` exits
  non-zero for a path a `.gitignore` rule ignores (having staged the others) and for one that is not there
  (having staged nothing); `commit_and_push` stops there, commits nothing, and the staging service rolls its
  clone back and removes the refused files — it used to commit the rest and report the proposal as staged. A
  caller that is not the staging service gets the request's paths taken back out of its index, so the part git
  did stage cannot reach `main` inside a later commit (`tests/unit/test_staged_write_lands.py`, real git). Every
  route answers a stage that did not commit with an error — the GUI's capability route answered 200 for one —
  and a test reads each function that calls the seam for it
  (`test_every_route_that_stages_reads_whether_the_stage_committed`; the route itself in
  `tests/integration/test_gui_api.py`). **This propose-only guarantee held for the `local` staging push but was
  breached by `commit_and_push`'s optional `origin` push (dogfood VM 144, 2026-07-05):** the content clone's
  `origin` **is** the canonical (a `file://` clone), so a client `push:true` ran `git push origin HEAD`,
  fast-forwarding canonical `main` from the network service unreviewed. **Closed structurally by `and not staging`**
  (`gitio.py` — the origin push is dead code while staging; the operator-CLI direct-main path is unchanged). The
  blast was FF-only because the clone is always rebased onto canonical `main` before the push
  (`_rebase_proposal_onto_canonical`), never a force — but it advanced `main` all the same. **Defence-in-depth:**
  the canonical carries an `update` hook (`ansible/playbooks/files/canonical-update-hook.sh`, installed by
  `local-canonical.yml`) that is an **allow-list of what a push may do**. As the **network-service uid (1001)**: a
  push may **create** `refs/heads/proposed/<run id>` — a minted run id, the same rule as `canon.valid_run_id` — and
  nothing else: not `refs/heads/main`, not a tag (a tag called `main` is what a short name finds first), not
  `refs/replace/*`, and not a second push to a proposal that exists (a proposal a human has read cannot be
  re-pointed by a push; a re-propose mints a new id). As **any other uid** — the operator, root in the installer
  container — a proposal (created or moved), and nothing else. **`refs/heads/main` is refused from everyone**
  (2026-10-19): nothing legitimate pushes it any more, so a hook that still let it through from some uid would
  let a hand-run `git push` skip the gate every other way in passes. A delete is refused from everyone, and so
  is every push when the pusher's uid cannot be read. A push can therefore leave one kind of ref there (a ref
  already in the canonical, or one written without a push, is not the hook's to refuse). The promote's
  `update-ref` is not a push, and neither is a landing's, so the hook never fires on either — a human promote
  and the deploy's landing are untouched. **What the hook does not stop** is said in its own header: a
  write that is not a push — `git update-ref`, or a file written under `refs/` through the `:rw` mount — runs no
  hook; that is the accepted-risk row for an armed box (`tests/unit/test_staging_isolation.py`: the argument
  table, the run-id samples fed to the hook and to the validator alike, and real pushes). **The promote is one ref transaction over two ids** (`gitio.promote_commit`): it
  verifies that the proposal is still the commit that was read and judged, and moves `main` only from the commit
  `main` was read as — so a promote that loses a race to another promote is refused by git itself instead of
  overwriting the winner, and a proposal re-pointed after it was judged is not promoted at all
  (`tests/unit/test_promote_cas.py`, real git). Design-of-record: `docs/reviews/2026-07-05-c10-origin-push-breach/` (a design record kept in the private repository).
  **No git this tool starts through the seam runs as root in a canonical the service can write.**
  The seam's environment answers the ways a repository makes git run a program or read another store that
  somebody has LISTED. The rest — the parser of a pack, of a ref file, of a config line, and whatever else those
  files can make a git do — is reached by whoever git runs as, and when a deploy is armed the canonical's files
  are written by a network service. So a caller with effective uid 0 (`sudo kontroll-promote`; any root caller
  of the seam) runs every git of the seam as uid:gid 1001 with no supplementary group, in a session of its own
  and started in `/` (`canon.git_kwargs`, rule 5 of `kontroll.canon`) wherever anything in the repository is
  that service's to write: whatever those files can make a git do, they make it do as the uid that wrote them.
  `canon.service_writable` asks the WHOLE repository — the top directory first, then every entry below it — and
  not one directory: an entry of a directory the service may write is something the service can exchange (for a
  link to a root-owned directory, for nothing, or — where one stands beside it — for a directory of somebody
  else's with no group write bit), and one look at `refs/heads` was talked out of its answer each way. The
  service's containers and the Semaphore job are that uid already and run as they are. A caller who is neither
  — a member of the canonical's group, without `sudo` — is refused by `kontroll-promote.py` and told to use it,
  the listing included (a maintainer's call; it is one refusal there). A root that may not change identity (a
  container without the capability) is refused in one sentence; it is never left to run git as itself.
  **What this covers:** the gits this tool starts through the seam — the promote, the listing, the discard, the
  export — and no other: a git started any other way runs as its caller, as before (the deploy's git in a
  content clone is the next paragraph's). The collection generator
  the gate runs over a copy of the proposal's files runs as the caller too: it starts no git, the copy is the
  caller's, the content is the proposal's. Held by `tests/integration/test_canon_drop.py` — as root, with real
  uids, on a canonical laid out as the play leaves one: a hook that writes down who it runs as, a terminal the
  child must not hold, a working directory the service may not enter — and, for the decision itself,
  `tests/unit/test_canon.py`.
  **The deploy's git in a content clone is the clone owner's** (`ansible/playbooks/tasks/content-clone.yml`,
  which `deploy-stack.yml` imports for the API's clone and for onboard-gui's). Armed, the service commits into
  its clone, so it writes that clone's config and hooks, and the deploy is root. So root runs no git in a clone
  the service can write, and changes no owner by path in a tree that uid writes. Armed, root makes the clone's
  directory, empty, and git run as uid 1001 clones into it and updates it — `setsid --wait setpriv --reuid=1001
  --regid=1001 --clear-groups --no-new-privs -- env -i …`: a session of its own (no terminal: ansible-core 2.18,
  the installer's, starts a task's command in the deploy's own session — on a `sudo` deploy, the operator's
  terminal), no supplementary group, no new privileges, an environment the task builds and a script the module
  does not rewrite from the deploy's (`expand_argument_vars: false`), started in `/`, and configured by one file
  root writes, which names the canonical safe and nothing else. Unarmed, the clone is root's, every file of it,
  and the git module updates it as before. **What stands at the clone's path is root's to place:** the clone's
  parent is root's in both modes, and so is the storage root that holds that file — the tasks refuse to go on,
  before anything in the clone is touched, unless each is a directory of root's that neither its group nor
  anybody else may write (a writer of the parent could exchange the clone for something else between the look
  and root's git), and that anybody may pass through (armed, the service's git — uid and gid 1001, no supplementary
  group — reaches its clone through both, and that file through the storage root, by the bit for others). The play
  makes a missing storage root root's, `0711`, before any store is made in it: Ansible's `file` gives the parents it
  creates the owner and mode of the path it was asked for, so the first store would otherwise make the root its own. A
  root that exists is not changed; one that is not root's alone, or that others may not pass through, is refused, with
  the fix. Under a `0711` root every local account reaches a store below it by its name, so a store's own mode is what
  closes it: the secret ones are closed (the captures `0700`, Loki `0750`, Vector's dir `0700`, the TLS dirs `0750`,
  the age key `0600`, and `config-loaded.json`, root's `0600`: a digest of each service's bound config, which covers
  the rendered `snmp.yml`, Vector's SSH key and the GUI's age key, so one would test guesses at the SNMP
  credentials), and those the containers read by the bit for others — the audit dirs and the Ansible run-log dir,
  `0755` — are open to every local account, as they already were under an installer-mode root (Docker makes it
  `0755`) and were not under a root an earlier build's first store made `0700`. A clone that is not wholly the owner
  the flag names — anything in it the service can write while unarmed (every entry is looked at, not the top alone),
  anything not the service's (root's, another uid's) or not a directory while armed — is removed (Python's rmtree,
  from open directories: a directory exchanged for a link is refused, a link at the top is unlinked, not followed) and
  its owner makes it again; the service is restarted after `up` so its bind mount is the new directory. It is never
  chowned: the earlier build chowned the armed clone recursively on every deploy, by path, in a tree the service
  writes, and told root's git to trust it with a system-wide `safe.directory` entry — both are gone. Held by
  `tests/integration/test_content_clone_as_service.py` — the task file run by ansible-playbook as root, holding a
  terminal, against a canonical of another uid's: programs the service planted in its clone (`core.hooksPath` hooks,
  `core.fsmonitor`) write down who they run as — uid and gid 1001, no other group, no new privileges, no capability,
  no terminal, no variable of the deploy's — and, for the structure, `tests/unit/test_content_clone_tasks.py`. **What
  it does not cover:** such a program runs as uid 1001 — the uid
  every network service runs as — with what that uid may read where the deploy runs (on a host-direct deploy, the
  host, outside any container, where it may leave a process behind; in the installer container, what the
  installer binds, and it ends with the container). It runs as nothing of root's; before this change it ran as
  root. An armed clone's update fetches `main` and checks it out: a change made in the clone and not proposed is
  discarded, and the deploy says so (the git module refused to update over one). Unarmed, root's git serves the
  canonical (the git module's fetch runs git's serving side there as root); nothing the service runs can write
  the canonical then — every mount of it is `:ro` — and the audit (below) names a key or a file an armed era left
  there. What a failure of these tasks says, and what to do, is
  [docs/troubleshooting.md](docs/troubleshooting.md) §9.
  **What is in a canonical that the play and the tool do not write is NAMED** (`python3 -B
  scripts/kontroll-canonical.py --audit <repo>`; `local-canonical.yml` runs it on every deploy, BEFORE its first
  write into a canonical that is already there, and prints what it names). A bare repository has many places
  where one more file changes what git does there — a config key, a hook, an entry of `info/` or of
  `objects/info/`, a ref of a kind the tool does not write, a ref that names another ref, a top-level entry the
  play does not make. The tool's own git sets its own answer for the ones an environment can answer (above); the
  audit is how a PERSON learns that one is there. It reads the repository as files, and from open directories:
  each directory is opened from the one above it and never through a link, and a file is read only when what was
  opened is a regular file — the directory may be written while it is read. It starts one process — git, handed
  the bytes of the config file on its standard input and told not to follow an include (from standard input it
  otherwise does), never told where the repository is; started by root for a repository the service can write,
  that git is the service uid. It prints a kind and a key per line and never a value: a key the play sets is
  COMPARED with what the play sets it to (missing, there twice, or turned is a line), and which value it has is
  not printed. Of owners and modes it reports the ones that decide whether the `update` hook runs and who may
  replace it, and no others; it does not scan the object store. Because it stands in front of the play's own
  writes, what the play is about to put right — `HEAD`, its keys, the hook — is named in that run, and a line
  that returns on the next deploy is something that changes the canonical between deploys. git's own housekeeping
  leaves nothing the audit names: the reflog of the tool's refs is the tool's own doing, and the play tells git's
  garbage collection to write neither `info/refs` nor a commit-graph, so one that is there is a finding. The
  play's own `git config` replaces every occurrence of a key: a second line of one of its keys neither stays in
  force nor stops the deploy. **It reports; it does not stop a deploy** — whether a finding ever should is not
  decided (`tests/unit/test_canonical_audit.py`: each planted thing named by kind and key, no value in the
  output, no link followed and no directory that is exchanged under the reader listed, a pipe not waited on, the
  one process held by a closed list, every kind in both tables, the play's tasks with their conditions evaluated;
  `tests/integration/test_canon_drop.py` for the uid).
  **`hooks/` is root:root `0755` and passed over by the gid-1001 write-grant** (`local-canonical.yml`; 2026-10-08
  review, finding 2; `tests/unit/test_local_canonical.py`) — kept. A promote does not rest on it: the seam runs
  none of the repository's hooks (`core.hooksPath`), and root's git there is the service.
  **The write-grant on existing entries is the ownership pass of `scripts/kontroll-canonical.py --normalise`**
  (2026-10-19): the audit's walker — each directory opened from its parent's descriptor with `O_NOFOLLOW`, each
  entry opened from the directory it was listed in — and the owner and the mode changed through the descriptor
  of the regular file or directory that was opened (`fchown`, `fchmod`), so the change lands inside the
  repository whatever a name in it is exchanged for meanwhile; a symbolic link is named and left as it is, its
  own owner too, nothing it points at is looked at, and a pipe, a socket or a device is named and never opened.
  `--check` (the play's detect task, run under the dry-run too when there is a canonical to look at) names what
  would change and changes nothing; a
  part the pass could not go over fails the task by name rather than pass in silence; on a platform without
  descriptor-relative operations the pass refuses instead of changing by path
  (`tests/unit/test_canonical_normalise.py`; with real uids, `tests/integration/test_canonical_normalise_as_root.py`).
  **After each staged push the service resets its content clone to the
  canonical `main`** (`_reset_content_clone_to_canonical`) so the read-back reflects the source of truth and each
  proposal is independent off `main` — a rejected proposal can't ride along in a later one (F1, dogfood
  2026-06-20; `tests/unit/test_staging_isolation.py`). **The promote now also runs a resolve gate (FIX-M9; it began as the pin-conflict gate):**
  before the fast-forward, `kontroll-promote.py` reads the prospective post-merge tree (`refs/heads/proposed/<run_id>` — the ref the fast-forward takes, by
  its full name: a tag, or a ref directly under `refs/`, that carries the same short name is not what it reads) and
  runs the TRUSTED tree's `gen-requirements.py --conflict-check --root <copy>` over that tree's DATA — never the
  proposal's own copy of the script, which until 2026-10-09 executed unreviewed code as root on the `sudo` CLI or
  with `SOPS_AGE_KEY` in scope on the Semaphore path (2026-10-08 review, finding 3); the subprocess inherits a
  whitelisted minimal environment (`tests/unit/test_kontroll_promote.py`). If two app-store units would pin one collection to
  conflicting `==` versions (which would make the NEXT deploy's `gen-requirements` fail closed, halting the image
  build far from the cause), it **refuses the promote** (exit 2; `--skip-conflict-check` is the documented
  override). The same gate refuses a tree whose fleet holds a line that is not a device-class key, with the
  generator's own
  sentence — the entry's place in the list, never the entry (C22) — because that generator would stop the next
  bootstrap or local-image deploy on it; and because it judges the whole tree, such a line already on `main`
  refuses EVERY proposal until one that removes it is promoted — the refusal then says that `main` itself is
  refused, and how to correct it there.
  Read-only (copies to a temp dir, never touches `main`). **The gate judges the commit itself.** The proposal's
  ref is read once, by its full name (`canon.read_ref`; a symbolic ref, or a ref that names no commit, is not a
  proposal and is refused), and the tree that is judged is that commit's: `canon.read_tree_verified` asks one
  `git cat-file --batch` for the commit, each tree and each blob, and **re-hashes every object against the id it
  asked for** — git does not check that an object it reads is the object its name says. Trees are parsed from
  their own bytes; regular files only. So the FILES the gate hands the generator are the commit's own, whatever
  the proposal carries and whatever else the repository holds: not an attribute (`export-ignore` hid a path from the old gate, which judged `git archive`'s
  export; `export-subst` rewrote a file for it), not a filter (a program the repository's config names — never
  run), not `info/attributes`, a replace ref or a commit-graph, not an object file swapped under its name. A tree
  that holds a **symbolic link, a submodule entry**, a name no checkout could take (a separator, a control byte,
  `..`, any spelling of `.git` that git's own checkout refuses, a name longer than a file system takes) or one
  name twice is **refused whole** — never skipped: a link in a promoted tree is a file some
  later reader opens *through* — as is a tree over the reader's caps. `tests/validate` holds the shipped tree to
  the same rule (no link, no submodule entry). `scripts/kontroll-export.py` is the same read for a play or a
  script that needs a commit's files in a directory — in place of `git archive | tar`. **It fails closed.** Each way the old gate answered "nothing to
  check" about a tree it had not read — the export failed, the unpack failed, no `modules/` came out, git or tar
  was missing — was a way to switch it off. Now a tree the reader refuses is a refusal (exit 2); a proposal with
  no `modules/` where `main` has one is a refusal (it would remove the device-class registry); and a gate that
  **cannot run** — the repository cannot be read, this checkout has no generator, the generator does not start or
  does not finish in its time limit — refuses with that sentence, which is a different one from "the tree is
  refused". "Nothing to check" remains for one case, decided on trees that were read: neither the proposal nor
  `main` has a registry. Two things that are not in the proposal's commit still take part, and each is refused
  rather than believed: a `main` that is a symbolic ref or names no commit refuses every promote (there is nothing
  to compare the proposal with, and nothing a promote should land on), and a tree with no fleet file of its own
  where `main` has one is refused (the generator would read this checkout's shipped example in its place). When
  the generator itself fails on a file of the proposal, its last lines are shown, bounded. `--skip-conflict-check` is the human's override for all of it — the never-brick path of a
  node whose gate cannot run — and nothing automatic passes it. The argument itself must be a minted run id
  (exit 1 otherwise), so nothing else is ever formatted into a ref name. `tests/unit/test_canon_tree.py`,
  `tests/unit/test_kontroll_promote.py`.
  **What is judged is what is promoted.** `kontroll-promote.py` reads `refs/heads/main` and the proposal ONCE,
  each by its full name; from then on it holds two commit ids and looks no name up again. The fast-forward
  question is asked of those ids; the preview it prints (the run id, the commit, the commits being landed) is
  listed from them; the gate judges that commit's tree; and `gitio.promote_commit` runs **one ref transaction**
  — `verify` the proposal is still that commit, `update` `main` from the commit it was read as, both under
  `option no-deref` — that lands exactly it or changes nothing. A proposal re-pointed
  after the read fails the `verify`; another promote that moved `main` fails the old value; neither can put on
  `main` a commit the gate did not judge. The proposal is then retired by compare-and-delete, also without
  following a symbolic ref. Every git of the promote runs in the seam (`canon.git_kwargs`): no hook and no
  config-named program of the canonical runs on it, and of the promoter's environment git is handed where
  programs and temporary files are and the locale, and nothing else — no `HOME`, no `GIT_*`, no key.
  **What a human confirms.** An approval names the proposal AND the commit: `<run id>@<commit id>`. The Pending
  panel's copy control yields that form for the row the operator read, and `kontroll-promote.py --list` prints
  it on the box; the promote refuses when the proposal has become another commit since ("not the commit you were
  shown"). A caller whose judgement of a proposal depended on what `main` was — a change list read against it —
  can name that commit too (`--onto <commit id>`): the promote then moves `main` from that commit or not at all.
  The `promote-proposal` job requires the pinned form (`--require-pin`); at the box's shell a bare run
  id is still accepted and promotes the commit the read returns. **The Pending panel is a convenience; the
  promoter's own printed preview is the evidence** — the panel is rendered by the service that staged the
  proposal, while the lines in the job log (or on the terminal) are printed by the promote itself, from the ids
  it lands. The listing is one record per commit that names its commit twice, read as committed
  (`--encoding=none`), and must open with the commit that lands (`canon.commits_between`). On the `sudo` path
  that record is the terminal's and nothing keeps it: an approval that must be on file goes through the job.
  `tests/unit/test_promote_cas.py`, `tests/unit/test_kontroll_promote.py`,
  `tests/e2e/test_pending_changes.py` (the copy control).
  **Every way into `main` lands; none is a push** (2026-10-19; rule 8 of the canonical's doors). `refs/heads/main`
  moves through two functions and no other: the promote (`gitio.promote_commit`, above) and the landing
  (`gitio.land_commit`). The doors that used to `git push … HEAD:refs/heads/main` — `scripts/update.sh` and
  `scripts/update-from-bundle.sh` after a code update, the operator CLI's onboard and capture-exception commits
  (`scripts/galaxy.py`), the deploy's mirror task (`ansible/playbooks/local-canonical.yml`), and
  `gitio.commit_and_push` outside staging (the GUI run from a checkout) — call it, through
  `scripts/kontroll-land.py` or `gitio.land_main`. A landing is: the worktree's HEAD as a commit id; the
  canonical's `main` read once, by its full name, in the seam; the fast-forward question asked IN THE WORKTREE
  (`merge-base --is-ancestor`: a canonical that holds commits the worktree does not — a promote — refuses, with the
  remedy and never `--force`); **the same gate the promote runs** (`_tree_refusal`: this checkout's collection
  generator over the commit's files, read object by object from the CALLER'S repository and re-hashed) judging
  the commit BEFORE anything touches the canonical — a fleet line, a collection pin conflict, a signature policy,
  a link in the tree, that used to be mirrored by every deploy and then refused every promote, is refused at the
  door, with the generator's sentence and exit 2; then the commit brought across as a **pack on a pipe**
  (`pack-objects --revs --stdout` in the worktree, no `--thin`; `index-pack --stdin` in the canonical, in the
  seam — which verifies every object's hash), checked whole there (`rev-list --objects` — a pack short of an
  object is refused before any ref names it), and **one compare-and-swap** (`update-ref --stdin`, `option
  no-deref`, from the commit `main` was read as; the zero id on a first seed, which creates `main` once). Why not
  a push: a push into the canonical runs its `update` hook and whatever its config names — `core.hooksPath`,
  `receive.*`, `uploadpack.*`, `remote.<name>.uploadpack` — as the **pusher**: root in the installer container, the
  operator on a host-direct run, in a repository whose config a network service writes when the deploy is armed
  (and a push passed no gate at all). A landing uses no transport: nothing of the canonical's runs
  (`tests/unit/test_land.py`: canary hooks and a `core.alternateRefsCommand` planted in the canonical do not run at
  any door, and the control shows a push runs them), and as root the canonical's side is the service uid (rule 5;
  `tests/integration/test_canon_drop.py`). The canonical's hook refuses a push of `main` from everyone, so a push
  by hand is refused too, with a sentence that names the door. `--check` judges and says what would land without
  changing anything — the deploy's dry-run runs it, so the mandated `--check --diff` names a refusal before the
  real run; `--skip-conflict-check` is the human's override, as for the promote, and nothing automatic passes it;
  `--propose <run id>` makes a proposal by hand (created once) — what the troubleshooting table used to do with a
  push. **What it does not cover:** the worktree's own git (`rev-parse`, `merge-base`, `pack-objects`) runs as the
  caller in the caller's repository — the operator's checkout, or the kit tree, which is the caller's to
  trust; a landing run by an operator without root on a host-direct deploy runs the canonical's side as that
  operator, in the seam, with no change of identity (strictly less than the push it replaces, which ran the
  canonical's hooks as them); a refused or failed landing leaves the received pack in the canonical's object
  store, unreachable, as a refused push did; and each landing adds one pack (`objects/pack/`) rather than loose
  objects — the canonical's garbage collection is the tool's to run, not a person's (`gc.auto` is off in the seam).
  Two more, measured on the play itself (Linux, as root, 2026-10-20): the mandated `--check --diff` of a FRESH
  node cannot judge the landing (there is no canonical to judge against until the real run creates one) — the
  play says so and judges on every run after; and what a landing by root leaves is the service's (rule 5) — the
  pack, the ref, the reflog — so the ownership pass runs a second time, after the landing, and re-owns them to the
  operator in the same run: the deploy after a landing reports 0 changed
  (`tests/integration/test_local_canonical_as_root.py`, the play run by ansible-playbook as root).
  `tests/unit/test_land.py` (real git at every door: the library, the command, `land_main`, `commit_and_push`
  outside staging, the two shell scripts under bash), `tests/unit/test_local_canonical.py` (the play's two
  tasks), `tests/unit/test_staging_isolation.py` (the hook: `main` from every uid).
- **Every GUI write surface rides this propose-only staging — including the Homepage editor (#136).** The
  onboard/capability/secret/keygen writes and the new `:8443` **Homepage editor** (`POST /api/homepage` — rearrange
  the portal tiles) all go through `gitio.commit_and_push(run_id=…)` → `proposed/<run_id>`; **none imports
  `promote_ref`** and there is **no promote button anywhere** in the GUI (verified by the `#133` read-only AST pin
  + the per-surface tests). The homepage editor serves no secret (its serializer can only relocate/omit existing
  `href:` tiles — never synthesize a tile or accept a token, C11) and edits only the tracked overlay, so it adds a
  new C10 *rider*, not a new trust boundary. The read-only Settings status panel (#135) stages nothing.
- **The generated Homepage Fleet tiles are secret-free by construction (#130).** `scripts/gen-homepage.py` fills
  the `# >>> kontroll fleet` span with `href:`+`description:` tiles only (one per onboarded host, derived from the
  inventory) — it never emits a `widget:`/`{{HOMEPAGE_VAR_*}}`/credential, and fails closed if a tile value holds a
  line break or a Homepage substitution marker (`{{HOMEPAGE_VAR_…}}` or `{{HOMEPAGE_FILE_…}}`: Homepage replaces
  those in the RAW text, before any YAML parser, with a variable's value or with the contents of the file it
  names — so no quoting helps). It does not judge a name by what it spells: a host, an address or a class key
  that merely contains a word such as `password` is a name (it used to stop the deploy). A refusal names the
  field and the rule, never the value (C11, the no-leak property the unauthenticated `:3000` board requires;
  `tests/unit/test_gen_homepage.py`). The editor
  treats that generated span as read-only (it refuses to edit its tiles), so neither the producer nor the consumer
  can introduce a secret into the board. The committed example span derives from `instance.example/` (TEST-NET),
  so the shipped bundle carries no real topology (F3 doctrine; the live span lives under the stripped `instance/`).
- **The app-store STAGE route is the newest C10 rider — the FIRST actuation write (R5).** `POST /actuation/{key}`
  (the `actuation.apply_actuation_plan` write verb) stages a configured unit's **non-secret** values file
  (`instance/actuation/<key>/vars.yml`) to `proposed/<run_id>` via the SAME `commit_and_push(run_id=…)` — never
  `main`; the route NEVER imports `promote_ref` (the Semaphore-admin promote is the trusted second key). It rides a
  **dedicated privileged prefix** (`/actuation` — token + the fail-closed audit + the per-token rate budget,
  distinct from the write-free `/units` inquiry reads), so a leaked token can only **stage a proposal**,
  never run a play (the API actuates NOTHING — a promote lands the unit, then the operator runs it in
  Semaphore). **No secret
  rides here** (a unit may not declare a secret-typed var — SEC-2), and the `actuation-stage` audit carries
  names/counts/run_id only, never a value. The write verb is registered in `WRITE_VERBS` **same-commit** (P0b/#134)
  and `build_actuation_plan` stays `assert_read_only`-pinned, so the read-only-by-construction boundary holds.
  Covered by `tests/integration/test_api_actuation.py` (401/404/propose-token/scoped-commit+audit/stale-409/
  proposed-ref). **The GUI now mirrors the STAGE route in-process** — `POST /api/actuation/<key>` (`gui/app.py`,
  the same propose→stage spine + `commit_and_push(run_id)`; preview `apply:false` renders the would-run play +
  resolved non-secret vars + a token, writing nothing; stage `apply:true` writes `vars.yml` to `proposed/<run_id>`)
  + the read-only `GET /api/actuation` **Automations index** (the registered units' derived target/pin/knob-count,
  NEVER a value; pinned `assert_read_only`). The `unit-config-stage`/`unit-config-result`/`unit-registry` audits
  carry names/counts/run_id only; the GUI **never** imports `promote_ref` / has no promote button. Covered by
  `tests/integration/test_gui_api.py`. The generated runnable artifacts (the Semaphore template/wrapper) + the
  **signed** `ansible-galaxy install` (SEC-1) are the follow-on rungs (R5 PR-B / R6).
- **The app-store CREATE route rides the SAME C10 spine — it AUTHORS a unit descriptor.** `POST /actuation/create`
  (the `actuation.apply_create_unit` write verb) renders an actuation `unit.yml` from a searched collection and
  stages **only** `instance/actuation/<key>/unit.yml` to `proposed/<run_id>` via the SAME `commit_and_push(run_id=…)`
  — never `main`, never `promote_ref` (the same dedicated `/actuation` privileged prefix + audit + rate budget). It
  fills the empty registry the STAGE/configure/preview reads depend on. **Two security properties are load-bearing
  here:** (1) the rendered descriptor is **validated through the SAME fail-closed `_actuation_schema.validate_unit`
  the generator enforces BEFORE it stages** (validate-before-stage), so a GUI-authored unit can never stage a
  malformed pin/target that the lockfile derivation would choke on; and (2) the **create-time Tier-cap** — the
  device-class + `inventory_group` are **derived from the declaring module, not client input**, and a collection
  whose class targets `edge_firewall`/`core_switch` is **refused (403)** because an unsigned public-Galaxy install
  may not reach the highest-blast tier (the never-brick ∧ blast-radius refusal; see C15-a). The flow also **never
  clobbers** an existing unit (409 on collision) and carries no secret (the `actuation-create` audit is names/counts
  /run_id only). The write verb is registered in `WRITE_VERBS` **same-commit** (P0b/#134) and
  `build_create_unit_plan` stays `assert_read_only`-pinned. Covered by `tests/integration/test_api_create_unit.py`
  (401/422/403-tier-cap/409-collision/scoped-commit+audit/stale-409/proposed-ref) + `tests/unit/
  test_create_unit_service.py`. **The GUI mirrors it in-process** — `POST /api/actuation/create` (`gui/app.py`, the
  same propose→stage spine + `commit_and_push(run_id)` + `_audit` the other GUI write dialogs use; the GUI **never**
  imports `promote_ref` / has no promote button) + the read-only `GET /api/units/<collection>` Pick-stage read
  (pinned `assert_read_only`); covered by `tests/integration/test_gui_api.py`. The GUI app-store **front-end dialog**
  (the `unit-badge` → pick → review → acknowledge → create chrome, reusing `renderCapPromote`; the Tier-cap surfaces
  as a loud refusal pane) is built — `tests/e2e/test_unit_flow.py`.
- **The discard route is a C10 rider that UN-STAGES, never promotes (FF-race Move 2).** `POST /api/pending/<run_id>/
  discard` (the `discard.discard_proposal` write verb, `scripts/kontroll/service/discard.py`) deletes a **stale**
  proposal's `proposed/<run_id>` ref from the canonical. It is C10-safe by construction: un-stage ≠ promote — it
  removes Key-1's OWN output, so it **cannot advance `main`, enact anything, or grow the promotable set** (the GUI
  still has no promote path / never imports `promote_ref`). The write verb lives in its OWN module so `pending.py`
  stays `assert_read_only`-pinned, and is registered in `WRITE_VERBS` **same-commit** (P0b/#134 — the fail-closed
  completeness gate fails CI until it is). **These security properties are load-bearing:** (1) **SEC-B1** the
  `run_id` validator accepts EXACTLY the two real minter shapes (`uuid4().hex[:12]`; the API
  `YYYYMMDDTHHMMSSZ-<6hex>`) and refuses a git flag / `../` traversal / branch name, and the ref is passed after
  `git update-ref -d --end-of-options` so a leading-`-` leaf can never be parsed as a flag; (2) **SEC-M1**
  compare-and-delete on the tip read first — a concurrent promote/rebase that moved the ref → **409**, a
  vanished/never-staged ref → **404**, a bad id → **400**, never a 500 or a blind unconditional delete; (3) **MF-4**
  the delete argv is a single literal list so the completeness detector sees the `update-ref` write; (4)
  **what is deleted is the ref of that name, never what it points at** — a ref under a proposal's name that is a
  symbolic ref, or names no commit, is refused as "no such proposal", and the delete passes `--no-deref`: what is
  deleted is the ref of that name, never a ref it points at, also if the ref changes kind after it was read. Audited
  (`pending-discard`/`pending-discard-result`: action run_id + target run_id + sha, names/sha only). **Accepted
  residual (availability, not integrity):** an authed GUI user can delete a legit pending proposal — the SAME trust
  tier the propose surface already grants; the control is **stale-only** + a two-step confirm (default griefing
  target ∅), and recovery is from the **audited sha** (`git update-ref refs/heads/proposed/<id> <sha>` before gc),
  not the reflog (a bare canonical keeps none for a deleted branch). Covered by `tests/unit/test_discard.py` +
  `tests/integration/test_gui_api.py` + `tests/e2e/test_pending_discard.py`.
- **The Pending list and the discard read the canonical by rules that are each one function of
  `scripts/kontroll/canon.py`.** The canonical is a repository the network service writes into, so what is *in* it
  is not something a reader may trust. (1) **A ref is named in full, read once, and is an id from then on**
  (`canon.read_ref`, `canon.proposals`). A short name is looked up: beside a tag called `main` the name `main` is
  the tag, and `proposed/<id>` is `refs/proposed/<id>` before it is the proposal — so the list compared, and
  printed, something other than the branch. The read returns the ref's own value with its type and says when the
  ref is a symbolic one; nothing is peeled and nothing is followed. A ref under `refs/heads/proposed/` that is not
  a proposal (a name the service does not mint, a symbolic ref, a ref that names no commit, a name for which the
  walk of the ref store and the look-up of that one name give different answers) is returned **apart** — shown in
  the panel, offered no control. (2) **Every git of the list and the discard runs in `canon.git_kwargs(<repo>)`**:
  one environment that names the repository outright, reads no other config scope, inherits nothing of the
  caller's but the program path, the temp directory and the locale, and answers the files it names through which
  a repository tells git what to run or what else to read: no hook, no replace ref, no graft line, no `shallow`
  file, no commit-graph; the config keys through which the verbs used here could start a program are overridden,
  and a write gives up on a held lock by itself instead of being killed over it. It does not answer the object
  store itself (an alternates file, an object swapped under its name) or the ref files. A repository that holds a
  `commondir` file is **refused**: `GIT_COMMON_DIR` keeps its config and objects its own, but git's ref store
  follows that file whatever the environment says; so is one whose `refs`, `packed-refs`, `logs`, `objects`,
  `HEAD` or `config` is a symbolic link. (3) **What git returns is bytes until it is shown** (`canon.show`): a
  path, an author and a subject are whatever was committed; a name that is not UTF-8, or holds a character that
  does not print (a newline, an escape, a direction override), is listed in an escaped spelling and marked, never
  raised on and never dropped — and a moved file is listed at both ends (`--no-renames`), against the commit the
  proposal was staged on. `exact` is about the bytes, not the look: a printable character can be invisible or
  resemble another, and no mark says so. **What could not be read is said, never shown as nothing:** a change list
  git could not print is flagged on its row and the row gets no promote recipe; a `main` that is missing or is not
  an ordinary branch is announced above the list and no row is called promotable against it; a ref git left out,
  and a canonical that could not be read at all, are each a warning in the panel. **Scope:** the Pending list and
  the discard. **Not covered:** a ref git itself does not list — a file under the prefix that holds no object
  name, a file whose name git does not accept as a ref name, a symbolic ref that points at nothing — is absent
  from this listing too (for the first two git says so and the panel passes it on; for the third it says
  nothing); one ref whose object git cannot read makes the whole list unreadable (the panel says the canonical
  could not be read) rather than one row; and the `commondir` and symbolic-link checks are made before a process
  starts — they catch a planted file, not a race. Needs git 2.31 or later (the overrides travel in
  `GIT_CONFIG_COUNT`, which an older git ignores silently). Held by `tests/unit/test_canon.py` (a test with a
  control — plain git IS fooled — for each entry a planted thing can be shown against: hooks, the replace ref, the
  graft line, the `shallow` file, the commit-graph; one test of the list itself for the entries no verb used here
  can show; and the run-id rule fed to the promote play's assert and to the validator alike), `test_pending.py`,
  `test_discard.py`, and the pin `assert_git_runs_in_the_seam` (`tests/unit/_readonly_pins.py`): in those modules
  every process is a git argv literal with an allowed verb that passes `**canon.git_kwargs(…)`.
- **No decrypting age key in the service.** Capability promotes carry no creds (telemetry/backup are data-only).
  Onboard's `sops_set` cred-encryption is deferred to the promote step (out-of-band, a trusted runner holding
  the scoped key) — because age can't encrypt-without-decrypt (1b §4.3), so **zero standing decrypt** sits in a
  reachable service (1b's headline risk, eliminated).
- **The trusted promoter (Semaphore) gets gated canonical write (item C).** The `promote-proposal` task
  fast-forwards `main` as **gid 1001** (forced `user: "1001:1001"`; the stock image is gid 0) against the
  canonical mounted **`:rw`** — but only when armed. **All three** network-service containers (api, onboard-gui,
  semaphore) mount the canonical `:${KONTROLL_CANONICAL_MODE:-ro}`, which deploy-stack renders `rw` only under
  `api_privileged`; the read-only deploy mounts it **`:ro`** on every container, so even a container RCE cannot
  write the canonical there (the `:rw` access needed for the proposal push / the promote exists only when armed).
  **No host root:** the promote rides the gid-1001 group-write grant + `core.sharedRepository=group`
  (local-canonical.yml). The promoter is **Semaphore, not the API** — by design the promote lives outside the
  token's reach. **The operator's own landings on the canonical need the same grant** — `scripts/update.sh`,
  `scripts/update-from-bundle.sh` and an onboard from the CLI land `main` as the operator (a pack and a ref
  transaction, never a push — C10 "Every way into `main` lands"), beside files uid 1001
  wrote there (the service's proposals; what a promote leaves is the service's too), which fails `Permission
  denied` unless the operator is a member of gid 1001. The CLI promote is no longer a reason: `sudo
  kontroll-promote` runs its git as the service uid, and without `sudo` an account that is not the service is
  refused (above). The named
  group + membership are provisioned by `local-canonical.yml` on a host-direct run, but it **skips** them inside the
  installer container (`KONTROLL_IN_CONTAINER=1` — both deploy launchers), where a container-local group is useless.
  So the **host floor** owns that gesture: `install-prereqs.sh` (root) creates it; the launchers ensure-or-warn
  (shared `scripts/lib/canonical-group.sh`) — closing the published-kit F-CANON gap (`task_51334deb`).
- **Unconditional hardening:** `receive.denyNonFastForwards` + `receive.denyDeletes` on the bare repo (guard the
  PUSH path — operator pushes too); a **dedicated push-only clone**, never a writable mount of the canonical
  content tree; a baked container git identity; rate-limiting (already in place). **Fail-closed coupling (item C
  hardening):** `api/settings.from_env` REFUSES to start a token-bearing API without `KONTROLL_STAGE_PUSHES`, and
  `gitio._push_target` RAISES if a staging service reaches a push without a `run_id` — so the propose-vs-`main`
  gate can't silently decouple into a direct-`main` write. Token stays SOPS-backed (`dashboards` domain — control
  + break-glass, **not** Semaphore-decryptable), constant-time, fail-closed, **mgmt-VLAN-only, never WAN** (C3).

**Proves (built with the enablement):** a contract test asserts the staged push targets `proposed/*` not `main`,
and that promote fast-forwards only; `git config -l` on the bare repo shows `denyNonFastForwards`; the deployed
compose carries **no** age-key mount (a `validate` assertion); the C9 audit gate is unchanged. **Uniform
staging (A2, 2026-06-15):** the GUI onboard route now also stages — it runs in-process (not the `main`-pushing
CLI) and threads `run_id` through `commit_and_push`, pinned by `test_gui_api.py::test_onboard_apply_stages_when_armed`.
So **every** network-surface canonical write (onboard + capability, API + GUI) proposes, with no shell-out hole.

**Residual (tracked):** for the **API** (no key), a leaked token stages proposals and holds no key to decrypt a
secret with. Under `separated` each proposal is operator-rejectable, and the residual is proposal-spam (rate-limit
bounds it) + denial-of-review-time; [docs/trust-mode.md](docs/trust-mode.md) says what `solo` changes. **onboard-gui is
the with-key exception:** it mounts the scoped age key for inline device-cred encryption (`sops_set` needs
decrypt-to-add), so a compromise of *it* (behind its own fail-closed auth, mgmt-VLAN-only) can read the
Semaphore-scoped secrets — a **pre-existing** exposure (the device-onboard alpha always held the key), now also
able to **stage** (bounded, rejectable) canonical-writes. The no-key property is pinned for `api.yaml` only
(`test_privmut_no_key.py`). HTTP onboard *with creds* is incomplete until the out-of-band encrypt step lands
(capability promotes carry no creds + are fully functional). Static token / no SSO is the C9 residual, unchanged.
**Arming (`api_privileged`) gives every network container `:rw` on the canonical (item C honesty).** When armed,
all three containers (api, onboard-gui, semaphore) mount `/srv/kontroll.git` `:rw` as gid 1001 against a
group-writable bare repo — so a **container RCE** (not a leaked token; the token is still propose-only) can run
`git update-ref` / rewrite `packed-refs` **directly**, which is **not** a push and therefore bypasses
`receive.denyNonFastForwards`/`denyDeletes` entirely. Those hooks and the FF-only gate bound the promote
**playbook**, **not** a compromised container: a breach of any armed container can **non-fast-forward or delete
`main` directly** (history rewrite), a higher-impact path than "park a rejectable proposal." This is **accepted**,
bounded by: the canonical is a **reconstructible cache** (the operator workstation + GitHub remain the source of
truth — a rewritten canonical is re-pushed and the divergence is visible); the **dominant** residual (fleet root
via the scoped age key onboard-gui/semaphore already hold) strictly dominates canonical-write; mgmt-VLAN +
single-operator; and a **deploy without `api_privileged` mounts `:ro` everywhere**, so this exists only while
armed. The leaked-**token** path remains strictly propose-only (the API never calls `promote_ref` / a direct
`main` push — the `from_env` + `_push_target` fail-closed coupling enforces it). Accepted as the cost of making
the one manual security-boundary step (promote) a GUI action.

**Phase-B bake-code STRENGTHENS this residual (M7).** When a service runs baked (`-e bake_code=true`), it imports
code from the **read-only, digest-pinned control image** at `/opt/kontroll` (`PYTHONPATH`/`working_dir`), not from
the writable `:rw` content clone. So an **armed container RCE that writes the clone can no longer inject the code the
service re-imports** — the in-process code-injection-persistence path that an un-baked clone-write had is moved behind
the image digest (the RCE cannot write an image layer). Rung 1b realizes this for the **api**; **Rung 2 extends it to
onboard-gui** — the higher-privilege surface (it holds the age key + the widest propose surface), so its in-process
code is now read from the digest-pinned image too, and its audit log moved off the `/repo` clone to a dedicated
`/audit` bind (M11) so the audit trail is writable under a read-only baked code root + survives the clone
`reset --hard`. **Rung 3 (the launch-kit / canonical shrink) realizes the next step:** the minimized kit that seeds the canonical
drops the **baked service code** (`api/ gui/`), so an armed-RCE canonical/clone rewrite has no `api`/`gui` source to
corrupt — the services run that code only from the digest-pinned image. The shrink is **conservative** (it keeps
`scripts/` whole, because the deploy-stack generators + the C10 promote slice + the **FIX-M9** resolve gate's
`gen-requirements.py` genuinely run *from the canonical clone* — dropping them would silently fail-open a never-brick
gate; closure adversarially verified, design-of-record §1). **M8a** (a MUST gate): the promote slice
(`kontroll/{__init__,gitio,paths}.py` + `kontroll-promote.py` + `gen-requirements.py`) is baked into the runner image
**and** kept in the kit/canonical, byte-identical by construction — so the trusted `kontroll-promote` FF-gate the
operator runs from the per-job clone can never be a *forked*, weakened copy of the AST-pinned baked code
(`tests/unit/test_launch_kit.py`). The remaining residual — `scripts/`'s non-promote service modules still ride the
canonical as inert (services run them only baked) — is a deferred finer strip, not a live exposure. The
`:rw`-only-when-armed mount + the FF-only promote are unchanged either way (payload-independent).

### C11 — Guided secret + key onboarding is no-leak by construction  ✓ implemented (rides the C9/C10 gate)

The guided **secret-entry** (`/secrets/{domain}`, `secret-forms/<domain>.yml`) and **key-generation**
(`/keygen/{role}`, `key-roles/<role>.yml`) surfaces let the operator mint service secrets and age keys through
the GUI so they are **encrypted/generated on the box and never hand-edited or committed in plaintext** (the
install-from-scratch "[B] security-boundary" steps). They ride the C9 auth/audit gate and the C10
propose-then-promote staging, but add their own **no-leak / additive** properties — the reason this is a
distinct control.

**Implements:**
- **Secret values never round-trip.** A submitted secret value flows through the service **in memory only**: it
  is encrypted into `instance/secrets/<domain>.sops.yml` via the **stdin whole-file** sops seam
  (`gitio.sops_write_domain` + `--filename-override` — never `sops --set`, so no value in argv / no temp-file
  cleartext) and is **never** returned, logged, audited, or committed. The client-safe plan VIEW and the audit
  line carry only field **NAMES** + a source label (`provided`/`generated`/`already_set`/`skipped`).
  `generate:` fields are minted server-side (CSPRNG) on a blank submit and never echoed. Idempotent: an
  unchanged domain re-encrypts to nothing (no canonical churn).
- **Generated private keys are show-once and never persisted by the service.** `/keygen/{role}` mints an age
  keypair (`gitio.age_keygen`, which prints nothing); the **private** half is returned in **exactly one**
  response for the operator to store (offline for break-glass; at the control/Semaphore key path otherwise) and
  is **never** written to the box, the repo, an audit line, or a log. The audit + the commit message carry only
  the **public** recipient.
- **The CLI's show-once path is the same.** `--mint-break-glass`, a choice `kontroll-init --fresh` takes, mints the
  break-glass keypair with the same primitive (`keygen.generate_keypair`, nothing on disk), adds its public half under
  the groups `key-roles/break-glass.yml` names (the dialog's), and prints the private half **once**, in a delimited
  block, straight after the recipient is written — before any later step can fail, so no recipient is left that nobody
  was shown. It refuses before minting anything when standard output is not a terminal, so the key cannot land in a pipe
  or a file; the installer container's log driver is `none` for every verb but a deploy (`init`, `check`: both launchers
  choose it per verb, `docker/services/installer.yaml`), and no launcher routes `fresh-init` through a run log, a `tee`,
  a recorder of the terminal (`script`, `asciinema`, `ttyrec`, `logsave`, `screen -L`, `tmux pipe-pane`, in the
  spellings `tests/unit/test_installer_phase1.py` lists), a runner that gives it a terminal of its own (`expect`,
  `unbuffer`, `rlwrap`, `socat`) or a command that files its output away from the terminal (`nohup`, `systemd-run`,
  `start-stop-daemon`). A terminal the operator records themselves is beyond kontroll's reach. `--break-glass-recipient`
  takes the public half of an X25519 key made elsewhere and, without echoing the value, refuses a private key, a plugin
  recipient (`age1<name>1…`: kontroll's images ship no age plugin) or a post-quantum one (it needs a sops whose age
  knows it on every machine), one whose checksum, X25519 length or padding fails, and a low-order point age refuses. A
  run that reads the control key file (`--break-glass-recipient`, or any `--fresh` run where something is at its path)
  refuses, before anything is written, one it cannot read a `# public key:` line from, and, where an `age-keygen`
  program is on its `PATH` (an alias, a shell function or a `PATH` entry written `~/bin` does not count; the installer
  has one), one whose line is not the public half of the key age reads there, or from which age reads no key (or where
  `age-keygen` gives no answer, one on `PATH` that cannot be executed included). In the installer container (known by
  `KONTROLL_IN_CONTAINER`, any non-empty value, or by the image's `/etc/kontroll/installer-image` or its dispatcher
  `/usr/local/bin/kontroll-installer`, which a `sudo` or an `env -i` in its shell cannot drop) any run first refuses a
  control key file that is not `keys.txt` in the directory of keys the installer mounts (`/root/.config/sops/age`,
  whatever `HOME` is), whether it is there or not, unless the run only reads it (`--break-glass-recipient` on an
  existing instance, and the file is there, a regular file: a link to no file or a directory is not): a key made in any
  other directory is lost when the container exits, or left in the checkout or another mount, after the instance has
  been written to it, and a key file of another name, made then or there before, is one no installer verb reads
  (`keys.txt` itself under another path or name, a link, a hard link or a second bind, counts as `keys.txt` once it is
  there); the refusal names each of these it finds (the variable, then the marker, then the dispatcher). On an existing
  instance it also refuses a key file that is not the control recipient of every rule (read as sops reads it: a rule's
  key groups when it has any), and stops after adding the recipient, before the bootstrap secrets.
- **`.sops.yaml` edits are additive + parse-verified.** Adding a key role writes only the **public** recipient
  into the named `/.sops.yaml` anchor group(s) — **anchor-preserving text insertion**, **idempotent**, and
  guarded by a **parse-verify gate** that refuses to write unless the edit is strictly additive (no existing
  recipient dropped → no unrecoverable secret; no unexpected recipient added → no over-exposure). The
  decrypt-needing re-wrap (`sops updatekeys`) is a **deferred operator step, never auto-run** (high blast); a
  scoped role lands in `ops_recipients` only (network+proxmox), never the service domains.
- **No standing decrypt key required.** Minting a keypair and adding a public recipient are **public-key**
  operations, so the keygen surface needs **no** age key — consistent with C10's no-key posture (the
  with-key exposure is unchanged: onboard-gui already mounts the scoped key for secret encryption).

**Proves (built with the surfaces):** `tests/unit/test_secrets_service.py` + `tests/integration/test_api_secrets.py`
(no value in any view/response/audit; missing-required 422; idempotent; no-key clean 500);
`tests/unit/test_keygen_service.py` (private key only in the result; `.sops.yaml` edit additive + idempotent +
anchor-scoped; the parse-verify gate rejects a dropped/smuggled recipient and writes nothing; `age_keygen`
prints nothing) + `tests/integration/test_gui_api.py` keygen cases (show-once response, audit/commit carry only
the public key, staged not `main`); `tests/unit/test_kontroll_init.py` break-glass cases (the minted private half
printed exactly once and in no file, shown even when a later step fails, refused on a non-terminal; a private,
malformed, mis-checksummed, plugin, post-quantum, low-order or non-zero-padding recipient refused unechoed, the
low-order check held to `cryptography`'s X25519 by `test_the_low_order_check_agrees_with_x25519_itself`; a key file
kontroll-init cannot read (empty, a directory, a FIFO, a link to nothing, not UTF-8) or one age reads otherwise,
refused on both paths, and at the path `--key-file` names
(`test_the_key_file_flag_names_the_file_every_check_reads`); in the installer container, a control key file anywhere but
the mounted `keys.txt`, there or not, refused first, unless an existing instance's `--break-glass-recipient` only reads
it (`test_the_installer_container_makes_a_key_only_where_it_mounts_keys`), the guard's constants held to the installer's
(`test_the_container_guard_reads_what_the_installer_sets`); a key file that is not the
control recipient refused;
the existing-instance run stopping before the bootstrap secrets) + `tests/unit/test_installer_phase1.py`
(`test_fresh_init_output_is_never_teed_to_a_log`, `test_the_installer_container_keeps_no_log`) +
`tests/unit/test_installer_log_driver.py` (both launchers, run, choose the driver per verb); `gitleaks` (CI) scans
every commit for an `AGE-SECRET-KEY` (C6).

**Residual (tracked):** a minted **private key** and submitted **secret values** traverse the mgmt-side hop in
the response/request body — protected by the surface's TLS (GUI) or the operator's reverse proxy
(`byo_proxy`, API), **mgmt-VLAN-only, never WAN** (C3), behind the C9 fail-closed auth. This is the **same**
plaintext-mgmt-hop exposure already accepted for secret entry; keygen is symmetric (a value out vs. in).
This cut ships keygen **GUI-only** (the smaller crown-jewel surface; show-once is human-interactive) — API
parity is an easy add if wanted. The **first** control key + `GUI_PASSWORD` are a CLI bootstrap
(`scripts/kontroll-init.py` — it writes the control private key to its canonical 0600 path, adds the public to
`.sops.yaml` via the shared parse-verified writer, and mints the `dashboards` bootstrap secrets, showing the
login passwords once), **not** this surface — a running GUI can't mint the key it needs to run (the keygen
split). kontroll-init runs locally as the trusted operator (no network surface), so its one-time write of the
control private key to disk is the legitimate exception to the never-persist rule.

---

### C12 — The logging pipeline is a secret surface (collector privilege, label hygiene, mgmt-only)  ◐ partial (internal stack built; deploy opt-in)

The dual-purpose logging pipeline (**Vector → Loki → Grafana**; [docs/logging-architecture.md](docs/logging-architecture.md))
adds a **privileged reader** (Vector tails every container's stdout (of the installer's verbs, only `init` and
`check` keep a log: C11), host journald, and the GUI/API audit logs)
and a **new operator-readable index** (Loki stream labels). Both are secret-adjacent: a log *line* can carry a
credential an upstream emitter failed to redact, and a *label* is queryable plaintext that must never embed a
secret. Logs are therefore treated as a secret surface, not telemetry. (This control lands with the **internal**
stack; the **capability** half — device-side wiring `no_log` + a per-method `secret_domain` — extends it.)

**Implements:**
- **No secret-valued labels (the hard label rule).** Loki stream labels are a **closed, non-secret set** —
  `source`, `host`, `service`, `level`, `run_id`, `device` — emitted at the single sink choke-point
  (`docker/vector/config.d/sinks/loki.yaml`) and asserted in each `logging/<method>.yml` `labels:`. No label
  value is ever a credential, token, or secret-bearing field; the set is fixed at config time, never derived
  from a log *value*. High-cardinality / sensitive fields (IP, user, message) stay in the JSON **body**
  (LogQL-filterable, not indexed).
- **Collector privilege is minimal + read-only.** Vector mounts the docker socket **`:ro`**, host journald
  **`:ro`**, and every audit/log path **`:ro`** — it reads, never writes the host, and actuates nothing on the
  fleet (a collector, not a controller — like the C9 exporters, a tail failure blanks a panel, not a device).
- **mgmt-VLAN-only, never WAN (C3 parity).** Loki publishes its HTTP API on the **mgmt IP only**
  (`${KONTROLL_MGMT_IP}:3100`, fail-closed `:?`, never `0.0.0.0`); **Vector publishes only the mgmt-bound syslog
  ingress** (`${KONTROLL_MGMT_IP}:5514/tcp`, fail-closed `:?`, never `0.0.0.0`) for the `syslog_push` method —
  the unauthenticated Vector API (`:8686`) stays loopback-internal, and Vector reaches Loki over the internal
  `kontroll` network; Grafana is the only query surface (mgmt-bound, NPM-fronted, C9). The existing
  `tests/check-mgmt-port-binding.py` covers Loki's bind, and `test_compose_logging.py` pins Vector's ingress to
  the mgmt IP + asserts the API is never published.
- **At rest (extends C8).** The **Loki store** is a **bind mount** at `/var/lib/kontroll/loki` (`0750`/uid-10001)
  — NOT a named volume — so the log bodies (which may carry a leaked secret) inherit the same operator-owned,
  runner-not-mounted, never-pushed crown-jewel treatment as the backup captures. Vector buffers are memory/named-
  volume only. `docker/vector/` config is non-secret config-as-data.
- **Bounded retention.** Loki's compactor retention is **enabled** (off by default = infinite) with a 30-day
  global window from `LOKI_RETENTION_PERIOD` (always set) — an empty/infinite period (unbounded disk) is the
  failure guarded by `test_compose_logging.py`.
- **The logging CAPABILITY (instance #3) extends the surface — config-injection + label hygiene fail-closed,
  device-side `no_log`, per-method SOPS.** `gen-logging.py` validates every `logs:` param against the method's
  CLOSED allow-list AND every contributed label against the canonical non-secret set — a metacharacter value or
  a secret-bearing label exits non-zero at generate time, never reaching the Vector config
  (`test_gen_logging.py`). The push/local methods (`journald_remote`/`syslog_push`) need **no** SOPS domain
  (like `host_node`); a credentialed method names a **`secret_domain`** in its `logging/<method>.yml`, rendered
  `no_log` into config at deploy, gitignored, audited by NAME only — `rest_pull`'s API token (an env), or
  **`file_tail_ssh`'s read SSH keypair** (`logging_file_tail`, rendered to a 0600 FILE + bind-mounted RO into
  Vector — the 3rd injection shape, never an env; see the dedicated bullet + accepted-risk row below). A
  device-side wiring role that templates a collector credential (a future TLS-syslog mTLS client key minted from the
  shared control-issued CA) carries `no_log` on that task — the C2 grep gate covers `ansible/roles/logging_*/**`. *(The device-side wiring roles —
  `logging_{syslog_push,journald_remote,file_tail_ssh}` + `ansible/playbooks/wire-logging.yml` — are now built:
  idempotent, `--check`-safe, access-chain-headed, with a `backup` rollback entrypoint and `no_log` on any
  credential task. The operator runs them `--check --diff` FIRST per the edge-firewall/core-switch hard rule.)*
- **The syslog ingress only listens when used + is mgmt-bound + reversible.** The published `:5514` is harmless
  when no class declares `syslog_push` (nothing listens inside the container → connection refused); a device
  starts shipping only after `logging_syslog_push` points it at the collector (a reversible config line —
  `no logging host`). Plain RFC5424 syslog is **unauthenticated** (see the accepted-risk row).
- **Remote journald PULL (`file_tail_ssh`) — a read-only forced-command key, gated custom image, mgmt-routed.**
  Vector PULLs a remote host's journald over SSH: its `exec` source runs `ssh root@<host>`, and the host's
  `authorized_keys` **FORCES** `journalctl -f -o json` with `no-pty` + no forwarding (`roles/logging_file_tail_ssh`),
  so the key runs ONLY that read command — no shell, no arbitrary file read (the tightest grant). The matching
  PRIVATE key is the **`logging_file_tail`** SOPS domain, rendered `no_log` to a `0600` file + bind-mounted **RO**
  into Vector (the 3rd injection shape — a key FILE, not an env). Because the stock Vector image has no ssh client
  (bench-verified), deploy-stack builds + selects a thin `kontroll/vector` image (stock + openssh-client, FROM
  pinned in lockstep with `vector.yaml`) **ONLY when a class declares the method** — every other instance keeps the
  stock pinned image. Host-key TOFU (`accept-new`) is mgmt-VLAN-only (see the accepted-risk row).
  `test_file_tail_ssh.py` pins the gating, the RO key mount, and the no-shell forced command.
- **Credentialed PULL egress (`proxmox_api`) — read-only, mgmt-routed, env-injected, never committed.** The
  first concrete credentialed pull: Vector's `http_client` GETs the PVE `/cluster/tasks` audit history read-only,
  authenticating with a `PVEAPIToken` in the `Authorization` header (`auth.strategy: custom`). The token
  **reuses the EXISTING audit-scoped pve-exporter token** — no new secret; it already carries only
  `Sys.Audit`/`PVEAuditor`, enough to READ the task history and nothing more. The token COMPOSITION is
  **data-driven, not hand-wired** — and UNIFIED across telemetry + logging: the method declares
  `secret_env_map: {ENV_VAR: "<str.format template over its secret_domain's SOPS field NAMES>"}` (the SAME block a
  metrics exporter uses; a 1-field map is the degenerate `"{field}"` case of an N-field token, so ONE filter renders
  both). `gen-secret-env.py` collects every telemetry `metrics:` + logging `logs:` method's `secret_env_map` into
  ONE **value-free manifest** (`config/secret-env.manifest.generated.yml` — env NAMES + domain NAMES + field-NAME
  templates, NO value), and deploy-stack composes each token into `.env` GENERICALLY: it decrypts the UNION of
  referenced SOPS domains **by name** (`instance/secrets/<domain>.sops.yml`, via the resolver seam — read-path ==
  the GUI onboarding write-path; fail-SOFT per domain) and runs the `kontroll_render_token` filter (`no_log`) in one
  loop. So a NEW credentialed exporter OR log-pull is a `telemetry/<m>.yml`/`logging/<m>.yml` `secret_env_map`
  drop-in — **zero `deploy-stack.yml` edit, no `_<domain>` lookup, no hardcoded domain map**; no method's token
  SHAPE lives in a hub. (A new *logging* token additionally needs a one-line value-free `${VAR:-}` passthrough in
  `vector.yaml` — compose `include` can't merge it; the telemetry path doesn't even have this.) **Platform-core
  boundary:** the control plane's OWN intrinsic creds (`SEMAPHORE_*`/`GRAFANA_*`/`GUI_*`/`KONTROLL_API_TOKEN`) stay
  explicit, **fail-CLOSED** `.env` lines from REQUIRED domains — they don't grow with the fleet, so they are NOT in
  the fail-soft device loop; a generate-time guard rejects any device `secret_env_map` that would shadow one. Passed
  to Vector as `${KONTROLL_PVE_LOG_TOKEN}`. The **generated config carries ONLY the `${ENV}` reference, never the
  value** (pinned by `test_gen_logging.py::test_proxmox_api_pull_renders_env_only_custom_auth`,
  `test_gen_secret_env.py` value-free + collision pins, `test_compose_logging.py`); the token is never a Loki label.
  Read-only: an HTTP GET reads the task history and actuates NOTHING on Proxmox (blast radius: control node only).
  **Live-proven read-only (HTTP 200, authorized); the unified render dogfood-verified end-to-end.**
  - **Onboarding now SURFACES the `PVEAuditor` grant prerequisite** (the `provisioning:` block, read by
    `catalog.module_provisioning`) instead of leaving it in a comment — but a role NAME is **public vendor
    vocabulary** (already in this bullet), projected NAMES-only and **never a credential**, so no trust boundary
    moves and no new control row. The surface is advisory/non-gating; the live drift check (C13, below) is what
    detects an actually-wrong scope on the wire.

**Proves (built with the stack):** `tests/unit/test_compose_logging.py` (Loki binds `${KONTROLL_MGMT_IP}` /
never `0.0.0.0`; Vector publishes ONLY the mgmt-bound syslog ingress `:5514` — never `0.0.0.0`, never the API
`:8686`; every Vector host mount is `:ro`; the store is a bind mount not a named volume; retention enabled +
bounded; the sink labels are the canonical non-secret set); `tests/unit/test_logging_wiring.py` (every method's
`wiring_role` is a built role with a backup entrypoint; the dispatcher + backend `configure` exist);
`tests/check-mgmt-port-binding.py` (C3, existing) covers Loki's `:3100`; `tests/validate` gains a `vector-config`
gate (`vector validate` over the config.d tree where the CLI is present).

**Hardened (the promotion blocker):** Vector runs **NON-ROOT** (`user: "10002:10002"`, a uid distinct from Loki's
10001 — `test_vector_uid_distinct_from_loki`) and holds **NO raw Docker socket** — its `docker_logs` source talks to
a `docker-socket-proxy` (GET/HEAD-only, `POST=0`) that alone mounts `/var/run/docker.sock`, so a Vector RCE cannot
`POST /containers/create` = host root. The proxy sits on its **own `internal` network** (`kontroll-socket`) with
Vector as its only peer: `GET /containers/<id>/json` returns a container's `Config.Env`, so on the shared stack
network any container could have read every peer's secrets through it (2026-10-08 review, finding 4;
`test_compose_logging.py`). The proxy's container root fs is intentionally **`read_only:false`** — the
pinned `tecnativa/docker-socket-proxy:0.3.0` renders its shipped `haproxy.cfg.template` into `haproxy.cfg` in its
own root fs at start, so `read_only:true` crash-loops it on every deploy (a committed-config bug an uncommitted
workaround masked until the VM-148 clean-redeploy detonated it); the real controls — the `POST=0` GET/HEAD filter,
`no-new-privileges`, portless kontroll-net-only reach, and the socket `:ro` mount — are unchanged, so the writable
container-private root adds no reachability. Pinned by `test_service_writable_root.py`. The stock `CONTAINERS=1`
grant passes every GET/HEAD under `/containers`: through it a real dockerd returned a file from a container
(`/archive`, `%2F`-encoded too) and its whole filesystem (`/export`), and upgraded a websocket
`attach/ws?stdin=1` (101), a write to the stdin of any container started with stdin open (2026-10-21). So the
proxy's start script first adds one `http-request deny` ahead of the template's allow lines: under `/containers/`
only the list (`json`), an inspect (`<id>/json`) and the logs (`<id>/logs`) pass, for an id that starts with a
letter or digit, and every other container read answered 403. It **fails closed**: if the line cannot go in, or
the copy the script wrote is anything but the template plus that line before the first allow line (a full disk,
where busybox awk exits 0 and leaves an empty or short copy), the proxy does not start (Vector's container logs
stop; the ERROR is in its `docker logs`). The script then adds one route: every `/containers/<id>/logs` request
(Vector's follow streams) goes to the template's own `docker-events` backend (`timeout server 0`), since the stock
`timeout server 10m` cut every idle stream. HAProxy applies the frontend's allow/deny filter before it picks a
backend, so the route changes no grant and `POST` is still refused (403 on a scratch box). The route fails open, under
the same kind of copy check: a copy that is not the limited template plus the route, on the line after the events route,
is not moved in, and the proxy starts without it. `test_compose_logging.py::test_socket_proxy_is_read_only_filtered`
pins `POST=0`; the script rewrites the filter's own config, so `test_socket_proxy_start_script.py` runs it on the pinned
image's whole template (vendored as a fixture) and holds it to adding those two lines and changing no other. The same
file evaluates the resulting filter rule by rule, under the image's whole ENV (recorded per tag) and the compose file's,
each variable it reads set to 0 or 1 (HAProxy's `-m bool` reads `true` as false and `2.2.33` as true), for each read
Vector makes and each read the limit denies. Overriding the entrypoint drops the image's CMD, so the script restates it,
and a test ties that line, the fixture and the recorded ENV to the pinned tag: a bump fails until the new image is
inspected and its template vendored. Host journald is read via
`group_add: ${KONTROLL_JOURNAL_GID}` (the host
`systemd-journal` GID, discovered by deploy-stack); the audit/run-log dirs via their `o+r` world-read — **NOT** via
`group_add: 1001`, which would over-grant read of the `0640 1001:1001` API/GUI TLS private keys (the MF-1 catch).
The file_tail_ssh key + its dir are rendered owned by the Vector uid (SSH demands it). Pinned by
`test_compose_logging.py` (`test_vector_runs_non_root`, `test_vector_uses_socket_proxy_not_raw_socket`,
`test_socket_proxy_is_read_only_filtered`, `test_vector_uid_distinct_from_loki`, the no-gid-1001 + the
`vector-data`-is-a-10002-owned-bind guards). **✓ GATE A PASSED (scratch-VM dogfood, 2026-06-18):** non-root Vector
(uid 10002, `group_add` the host systemd-journal GID) shipped **1822 host journald events** + container logs
through the proxy (0 errors), and `POST /containers/create` via the proxy returned **403** (create blocked). The
dogfood also caught that the data_dir must be a **10002-owned bind** (a named volume inits root-owned → non-root
Vector couldn't write its checkpoint) — fixed. **Promotion unblocked.** **Residual (tracked):** Vector still ships
**plaintext** log bodies that *may* contain a secret an upstream device logged (the stack's own logs are
`no_log`-disciplined, C2; Loki is mgmt-only, C3). Loki has **no auth** at homelab scale (mgmt-bound, Grafana-only
query path) — add auth before any wider exposure (residual 5, deferred).

### C13 — The online-validate seam is a read-only, no-new-port reaching client  ◑ partial (built; control-VM/on-demand)

The no-bespoke tenet's VALIDATE leg (`scripts/gen-validate-live.py`) reaches each enabled class's hosts to confirm
a pinned vendor fact (TLS posture, API path, token read-scope, SNMP OID support) still matches the live device.
Reaching the lab is the point, so the trust posture is bounded by construction:
- **No new listening port + no new credential.** It is an OUTBOUND client (like the exporters / the Vector pull),
  not a listener. It **reuses** the existing audit-scoped `proxmox` token (read-only, `Sys.Audit`/`PVEAuditor`) and
  the `snmp_observability` SNMPv3 USM user — no new secret, no new SOPS domain. The token is only an `${ENV}` ref
  resolved at the fetcher boundary; the audit line + the on-screen verdict carry the secret-domain **NAME** only,
  never the value (C2/C12 by NAME — pinned by `test_gen_validate_live.py`).
- **The TLS 1.2 floor, stated.** Every outbound TLS context the seam builds goes through one constructor
  (`LiveFetchers._tls_context`) that pins `minimum_version = TLSv1_2` explicitly — the default on every supported
  Python, written down so an interpreter build or an `OPENSSL_CONF` cannot lower it (CodeQL `py/insecure-protocol`;
  `test_gen_validate_live.py` pins that no other line builds a context).
- **A closed read-only transport allow-list.** The reaching primitives are a TLS handshake, an HTTP **GET**, and an
  SNMP **GETNEXT** — there is no write/POST/SET primitive, so a check **cannot** actuate a device. Machine-enforced
  by a `tests/validate.sh` `validate-live-readonly` grep-gate (a write verb cannot be declared) — the
  `exporter-no-host-port` idiom.
- **Reaches only already-allowed C3 paths.** The MVP reaches proxmox `:8006` over the SAME mgmt-routed paths the
  Vector pull + the pve-exporter already traverse daily, plus the device's own node-local apidoc (`:8006`, NO WAN
  egress) — zero new device-reaching path, zero new egress. The high-blast firewall/switch SNMP reach is deferred
  (its per-validator access-chain header attaches when it lands).
- **Tier: control-VM / on-demand, never hermetic CI.** It is deliberately absent from the CI/`validate` lane (it
  reaches the lab); its dispatch + judgement are proven hermetically via an injected fake fetcher. No trust
  boundary moves — it reads what the daily pipeline already reads, and writes nothing.

**Proves:** `tests/unit/test_gen_validate_live.py` (vendor-blind dispatch, reach-then-judge verdicts, token resolved
from the descriptor's own env never an admin side-channel, the token value never in the audit/verdict, the
200+empty silent-trap disambiguated, fail-closed on an unknown check); the `validate-live-readonly` grep-gate in
`tests/validate.sh`. **A′ (the schema-permission check) LANDED** as `check_schema_permission` — reshaped after a
live finding (`/cluster/tasks` GET is `{user: all}`, row-audit-filtered, so the original "required-permission"
form was moot): it now pins the apidoc's permission MODEL (`expect: {user: all}`) and DRIFTs if PVE tightens the
endpoint, a deterministic read-only apidoc GET (no role literal, MF-4; no new surface). **The heavier
role→privilege-map A′ LANDED too** — a live apidoc probe (2026-06-17) confirmed a real perm-gated dependency:
`/cluster/status` GET requires a hard `Sys.Audit` check (`{check: [perm, /, [Sys.Audit]]}`), exactly the permission
the provisioning surface's **PVEAuditor** grant confers and the proxmox metrics path depends on — and nothing
validated it (`token_scope` only exercises the `{user: all}` pull). Closed in two halves on `logging/proxmox_api.yml`:
`schema_permission` with an `api_path` override pins that the apidoc still REQUIRES `Sys.Audit` (catches PVE
relaxing/changing it — least-privilege drift); new **`check_grant_covers`** LIVE-reads `/cluster/status` with the
descriptor's own `*.Audit` token (200 ⇒ the grant confers it; an UNAMBIGUOUS 403 ⇒ the grant lost coverage and the
privileged reads break). Both judge only schema/HTTP-status — the role/permission are descriptor DATA, never a check
literal (MF-4 preserved). This closes the loop between the onboard provisioning grant and the validate seam. Live OK
2026-06-17.

### C14 — Capture-read over the GUI is a read-only, `:ro`-mounted, redacted, audited surface  ◑ partial (built; G1–G5 cleared 2026-06-24; redacted-only MVP, not yet live-deployed)

The onboard-gui gains a **read-only** per-device backup index → single-rev view → unified diff over the
secret-bearing config-capture store (the same `0700`/uid-1001 git history C8 owns). Serving captures over HTTP is a
new read path for crown-jewel secrets (running-config hashes, PSKs, SNMP communities), bounded **by construction**:
- **`:ro` mount, onboard-gui ONLY.** The captures dir (`${KONTROLL_BACKUPS_DIR}`) is bound **read-only** into
  onboard-gui at `/backups` — a GUI RCE can READ, never rewrite/`git rm` the history. Scoped to the one surface (the
  API container does not mount it — one surface, one justification). Pinned by a compose assertion on the literal
  `:ro` suffix (`test_backups.py::test_onboard_gui_mounts_captures_read_only`) — a bare `source:target` defaults to
  `:rw`, so the test asserts the suffix is PRESENT, never merely that `:rw` is absent (the one silent fail-OPEN).
- **Read-only by construction.** `service/backups.py` shells only `git log/show/diff/ls-files` — no write verb. The
  AST pin `_readonly_pins.assert_read_only` + the argv git-verb pin `assert_no_git_write` fail CI if any mutating
  subcommand ever appears (a `git rm` riding a subprocess arg list is caught on the verb, not just the call name).
  No restore / rollback / prune from the GUI. Those pins fail OPEN on a verb the registry doesn't know, so a
  **fail-CLOSED completeness gate** (`assert_write_verbs_complete`, P0b/#134) AST-scans the whole service+gitio
  layer and breaks CI if any DIRECT mutator (write-open / git-mutating argv / SOPS write) is not in `WRITE_VERBS`
  — keeping the pin EXHAUSTIVE (its 2026-06-24 sweep closed a real gap: `gitio.sops_write_domain`, an unpinned
  secret-writer). Since 2026-10-12 `assert_read_only` is **transitive**: it follows the pinned function's helpers
  (called, or passed on as a callback) and the functions of other repository modules it reaches through an
  import, and looks in each for a registered verb and for the write primitives themselves. A read view can no
  longer be un-pinned by splitting it into helpers or by moving the write behind an unregistered function of
  another module, and a pinned function that writes with a primitive no verb names is refused even if it was
  registered. What it cannot follow is dynamic dispatch (a computed `getattr`, a function passed in as data) —
  that stays with review. This read-only-by-construction guarantee underpins the C9/C10 privileged-surface read
  views too.
- **Strict input validation BEFORE any `git show`.** `rev` is a 7–40-hex SHA; `file` must be a MEMBER of the
  store's own index allow-list (stronger than a path regex — no `..`/`/`/`-flag`/NUL can be a real capture name);
  `--` precedes every path; subprocess is an argv list (never `shell=True`); output is byte-bounded. Path-traversal
  + rev-injection unit tests assert a bad input never reaches `git show`.
- **Redacted by default; redact-then-diff.** The in-browser view/diff mask known per-vendor secret token SHAPES
  (registry `config/capture-redactions.yml`, validated against the real lab vendors) server-side before the bytes
  leave the process; the diff runs on the redacted text so a pure-secret change shows `‹redacted› → ‹redacted›`,
  never leaking that *a* secret changed. **Redaction is defense-in-depth, NOT the boundary** — the boundary is the
  GUI's auth + TLS + mgmt-VLAN bind (C3) + the `:ro` mount.
- **Inside the inherited gate, every read audited.** Behind the GUI's fail-closed Basic auth + TLS + mgmt-bind
  (C3); every index/revisions/view/diff read writes a NAMES-only audit line (`backup-{index,revisions,view,diff}`,
  file + rev, **never a capture value** — pinned by `test_backup_view_redacts_and_audits_no_value`).
- **Graceful-absent, never fail-open.** A missing captures dir / `.git` → `{available:false}` (HTTP 200), never a
  crash or empty-success; the service never `sys.exit`s the worker. The feature is *inert*, not broken, on a node
  without the mount.

**Proves:** the compose `:ro`-suffix assertion; the AST + argv read-only-git pins; a redaction regression over
**real lab vendor token shapes** (Cisco type-7/9 + SNMP + key-string, FortiGate `set <k> ENC`, OPNsense
`<password>`, RouterOS `radius-password=`/`wpa2-pre-shared-key=`) with an over-redaction guard; the
audit-no-capture-value route test; path-traversal / rev-injection unit tests; the absent-store → 200 test.

**Residual (tracked):** Redaction is pattern-based and **best-effort** — a novel vendor secret token shape (a new
FortiGate `ENC` field, an unseen `$<n>$` hash form) can LEAK into the in-browser view. Line-oriented redaction also
**structurally cannot mask a MULTI-LINE secret body** (a PEM private-key block — its base64 body lines carry no
keyword); the registry flags the `-----BEGIN … KEY-----` opener best-effort, but the only vendor that emits
multi-line PEM bodies in captures (FortiGate) is history-EXCLUDED → never rendered (below), so this limit is latent.
The shipped surface narrows the residual concretely: the secret-densest, history-EXCLUDED vendor (**FortiGate** — its full-config export re-serialises
~570 lines/fetch and is `exclude_from_history`, capture-exceptions.yml) is **listed but NEVER rendered** by the
viewer (the MVP serves TRACKED history only — its on-disk-only blob never reaches the wire), so the leak is bounded
to a novel token shape inside a *tracked* capture, behind fail-closed auth + TLS + mgmt-VLAN-only (C3, never WAN).
The marginal exposure is dominated by the existing onboard-gui-holds-the-age-key residual (C10 — a GUI compromise
already owns fleet root, so capture-read is additive-within-maximal-compromise, not a new pivot). The raw
(unredacted) download path is **deferred** (C2 — its own audit + UI confirm + security pass). **Ship gate (G1–G5):
G1** `:ro` compose-pinned · **G2** traversal/injection closed before `git show` · **G3** registry validated vs real
captures + this FortiGate residual named · **G4** no capture value audited · **G5** a HUMAN security sign-off on
this control + the mount (this build is the first pass; the operator sign-off is the second). The G5 signer should
accept, eyes-open, the two named residuals: redaction is **best-effort** (a novel single-line token shape can leak)
and **line-oriented** (it cannot mask a multi-line PEM body — latent, since the multi-line vendor FortiGate is
never rendered). The 2026-06-24 adversarial review (`docs/reviews/2026-06-24-backup-viewer-c1-review/`) confirmed
G1–G4 hold in code and the FortiGate "listed-but-never-rendered" claim is true at both the GUI gate and the service
layer (an excluded on-disk file has no commit, so `git show <rev>:<file>` cannot serve it). **G5 GIVEN 2026-06-24**
— the operator signed off on this control + the `:ro` mount, accepting the two residuals (best-effort single-line
+ line-oriented redaction) eyes-open. All five gate items pass; the redacted-only MVP is cleared to ship (raw
download stays deferred to C2; the surface is inert until a node mounts the captures store).

---

### C15-a — Supply-chain install is NEVER-BRICK: adaptive pinning + checksum, signature-where-served  ◑ partial (built 2026-06-25: the trust sidecar + descriptor Tier-cap + offline generator + the I-1 bootstrap AND I-2 runner-image install wrappers, keyring-gated + the I-1≡I-2 no-drift gate; **the L2b recorded-sha256 re-verify now binds on the control node** — pre-fetch→verify→`--offline`, TOFU lock, fail-closed on mismatch; keyring auto-provisioning remains the inert next rung)

A novice's bootstrap / runner-image build / deploy **must never fail because some upstream publisher didn't sign a
collection.** Live-confirmed (2026-06-25): public `galaxy.ansible.com` serves **zero** GPG signatures for all six
fleet collections and a `sha256` for every one — so a fail-on-absence (`+`-prefixed) signature flag would brick
100% of the fleet. The control is a three-layer posture (design of record: `docs/reviews/2026-06-25-never-brick-supply-chain/`):

- **L1 (unconditional) — exact `==` pin** where a unit declares it; the resolved set rides the generated lockfile.
- **L2 (unconditional, brick-proof) — checksum.** `ansible-galaxy` verifies every file vs `FILES.json` for free
  (L2a — internal consistency only; a self-consistent full repackage passes it). **L2b — the whole-artifact digest
  floor** (built, control node): the install wrapper pre-fetches each tarball over cert-validated HTTPS, computes
  its `sha256`, and TOFU-binds it in a gitignored per-node lock (`instance/trust/observed-digests.yml`) keyed by
  exact `name==version` — **first install records, a later mismatch fails closed** — then installs from the verified
  local file with `--offline` so the bytes verified ARE the bytes installed (MF-2). The digest is recorded by the
  **install wrapper at first install, never fetched by the generator** (synthesis Decision R1: a network GET in an
  install-gating, hermetic generator would itself brick on a Galaxy blip / air-gap). **L2b engages only where its
  persistent lock can live** — `instance/trust/` on the control node (I-1 bootstrap); the runner image (I-2, no
  `instance/`) falls back to the plain L2a install. The gate is loud (a green install never implies L2b bound when
  it didn't).
- **L3 (adaptive) — GPG signature. Dormant: no keyring is provisioned, so no install has run with these flags.**
  When a keyring file exists at the sidecar's path the wrapper adds `--keyring … --required-valid-signature-count
  all --ignore-signature-status-codes NO_PUBKEY NODATA` — the count that passes when a collection serves **no**
  signature and fails when one it serves does not verify, never the `+`-prefixed count that fails on absence. What
  that branch does with a real served signature is **not established**: it has no test beyond the command line.
  The ignore-list is fixed at `{NO_PUBKEY, NODATA}` and **never** widens to a tamper code.

**Never-brick invariant.** Absence of a signature never halts an install; only **positive evidence of tampering**
(a signature that fails to verify, or a recorded checksum that mismatches) does. The default (`adaptive`) needs
**no novice configuration**. **`required` is, today, a declaration and not a mode of the install:** the wrapper
builds one command line whatever a collection's policy says — with a keyring, `--required-valid-signature-count all`
and the two ignored codes, never the `+`-prefixed count that fails on an ABSENT signature; without a keyring, a
plain install — so a collection declared `required` installs exactly as an `adaptive` one does. The policy is
written into the trust sidecar, counted in the wrapper's summary line and shown by the provenance view, which
paints `signed` for "declared `required`, and a keyring is held" without having seen a signature; nothing else
reads it. Making it fail on absence is the ratchet the un-defer trigger below describes, and it is not built.
**Tier-cap:** a downloaded unit whose descriptor does not declare `signature: required` may **not** target
`edge_firewall`/`core_switch` — `gen-actuation.py --check` rejects the combination fail-closed (never on plain
absence), and the **app-store CREATE seam** (`POST /actuation/create`) refuses it earlier still, at author time
(**403**), keying off the module-DERIVED `inventory_group` (not the operator's blast label) so a downgraded blast
can't smuggle an unsigned install onto the high-blast tier — see the C10 create rider. **The cap is a rule about
what a descriptor DECLARES, checked where descriptors are validated** (a known limit, written down 2026-10-12;
nobody has accepted it). It is checked in `tests/validate` and as the create route's last step, and nowhere else:
bootstrap, deploy and promote do not run it. And since `required` changes nothing at install, a unit written
outside the create route — by hand, or in a proposal a human promotes — that says `required` passes it, and its
collection — unsigned, on public Galaxy — installs like any other. The create route does not lean on the word: it
takes no signature at all, writes `adaptive`, and answers 403 for a class on those two groups. Nor does the cap
ask which collection a unit installs: a unit aimed at a group it does not guard may pin a collection that a
guarded class's own plays use, and an exact pin wins over the module's floor. None of this is how unsigned code
first reaches those two devices — the classes' own collections are unsigned and always were (R-NB-1); the cap is
about what a unit ADDS, and what a unit adds is promoted by a human. Whether a unit written outside the create
route may reach those two groups before `required` is enforced is a decision still open.
**The cap compares group NAMES** (a known limit,
written down 2026-10-12; nobody has accepted it): a parent group an instance defines over those two — the shipped
example inventory has one, `network_devices` — is neither name, so the play of a unit attached to it addresses
both devices and the cap does not ask; nor does it ask about a unit with no target group, whose play is rendered
against `all`. What would close the first is a cap that reads group MEMBERSHIP: it needs the inventory, and it
changes what this control names as its mechanism, so it is a theme with a review of its own. The second is
listed with C22's known gaps; whether such a unit must then carry `signature: required` is a decision still open.

**Built so far:** (1) **the data foundation** — `scripts/gen-requirements.py` emits the trust sidecar
(`ansible/collections/trust.generated.yml`, gitignored) with `default_signature_policy: adaptive` + per-collection
policy + `sha256` placeholders, staying **offline**; `scripts/gen-actuation.py` adds the `adaptive` enum default +
keyring/`sha256` shape validation + the Tier-cap. (2) **the I-1 (bootstrap) install wrapper** —
`scripts/install-collections.py` replaces the raw flat `ansible-galaxy collection install` at `bootstrap.yml`: it is
**keyring-gated** so **no keyring on disk ⇒ a plain `-r` install, byte-identical to the prior behaviour** (the
current all-unsigned fleet has zero behaviour change / zero brick risk), and a keyring present ⇒ the adaptive L3
flags (`--required-valid-signature-count all --ignore-signature-status-codes NO_PUBKEY NODATA`, **never** a
`+`-prefixed count). It prints a loud, names/counts-only provenance summary. (3) **the I-2 (runner-image) install
wrapper** — `docker/semaphore-runner/Dockerfile` installs via the **same** `scripts/install-collections.py`
(env-parameterized for the semaphore-venv `ansible-galaxy` + the `-p /usr/share/ansible/collections` target) and
COPYs the trust sidecar (BRICK-2), with a `test_dockerfile_installs_via_the_same_wrapper` **no-drift gate** pinning
that both seams use the identical wrapper (no `+`-flag can slip onto one path only). (4) **the L2b recorded-sha256
re-verify** — the wrapper now diffs the lockfile against what is installed (`collection list --format json`, local,
so a present pinned set does **no** fetch: air-gap-safe + idempotent), pre-fetches only the to-install subset,
TOFU-records/re-verifies each tarball's `sha256` against `instance/trust/observed-digests.yml` (fail-closed on
mismatch), and installs `--offline` from the verified files. `instance/trust/keys/.gitkeep` keeps the trust dir
present (FIX-KEYS). (5) **the MF-5 provenance surface (no verification theatre)** — `service/provenance.fleet_provenance`
(read-only) classifies each collection `unsigned-pinned` (amber — verified by pin + checksum only, no signature) or
`signed` (green — policy `required` AND a keyring held), surfaced in the GUI Index 'Supply chain' section
(`GET /api/provenance`, audited counts-by-class) so an amber row never passes for a checked signature; a green
`signed` row means "declared `required`, keyring file present" and not that a signature was seen (the limit
above). **Next
rung (inert until a signing-enabled source exists):** keyring auto-provisioning so a *signed* collection actually
verifies (today's fleet serves zero signatures, so the L3 keyring path stays dormant).

**Proves:** `tests/unit/test_actuation_registry.py` (the sidecar defaults every module collection to `adaptive` +
null `sha256`; the Tier-cap rejects unverified high-blast incl. on absent signature; a low-blast `adaptive` unit is
valid; the **BRICK-1 no-network source pin**); `tests/unit/test_install_collections.py` (no keyring ⇒ plain install;
a keyring ⇒ the adaptive flags; **never a `+` count**; the ignore-list is EXACTLY `{NO_PUBKEY, NODATA}`, never a
tamper code; an install failure propagates fail-closed; **L2b: the present-set re-run does no fetch (air-gap/idempotent),
a fresh install records the tarball `sha256` then installs `--offline`, and a recorded-digest mismatch fails closed
WITHOUT installing**); `tests/unit/test_bootstrap_requirements.py` (bootstrap
installs via the wrapper, not the raw flat command); `tests/validate` `gen-requirements --check` (hermetic staleness
gate, the only `gen-*` that lacked one) + `gen-actuation --check`.

**Residual (R-NB-1, accepted — the conscious never-brick trade):** on public Galaxy the fleet is verified by `==`
pin + `sha256` only — no publisher-identity proof, because the ecosystem serves nothing more. L2b closes
**whole-artifact swap of an already-recorded `name==version`**; it does **not** close **version-bump on a `>=`
floor** (a malicious *newer* release resolves, installs, and TOFU-records as a *new* entry) — that is closed only by
an exact `==` pin, so the floor's version-bump stays the accepted residual until the operator pins `==`. The
first-install of a not-yet-recorded pin is a **TOFU window** (R-NB-2), bounded by cert-validated HTTPS + the
human-add `modules/` gate. Bounded overall by the pin, the recorded checksum and loud surfacing; the Tier-cap
narrows what a unit authored through the create route can add for the edge firewall and the core switch, and is
a declaration for a unit written any other way (the known limits above).
**Un-defer trigger:** a signing-enabled source (Automation Hub / a self-signed mirror) becomes available → ratchet
`signature_policy: required` against it — which then has to be built: today the word changes no install (above).

---

### C16 — The compose-native installer is an ephemeral, host-bind, host-root-trusted surface  ◑ partial (Phase 1 built; fresh-VM dogfood pending)

Phase 1 of the compose-native install (docs/reviews/2026-06-29-compose-native-install/) moves the install
*compute* into an ephemeral container (`docker/installer/Dockerfile` + `docker/services/installer.yaml`, launched
by `scripts/kontroll-installer.sh`) that runs the unchanged bootstrap/local-canonical/deploy-stack playbooks. It is
**host-root-equivalent for the run's duration** (a `:rw` docker socket = the host daemon), so the floor is:

- **No secret in an image layer or a named volume.** The age key, the C10 canonical, and the rendered `.env` 0600
  are **host bind** files — a named volume is docker-socket-readable (strictly wider than a 0600 host file), so it
  is forbidden. The image bakes **no** secret / age key / `instance/`; the repo-root `.dockerignore` is the guard.
- **Ephemeral, CLI-only, portless.** `run --rm`, no `restart:`, no `ports:` — the socket grant exists only while
  the one-shot installer runs, never as a standing surface.
- **`SOPS_AGE_KEY_FILE`, never the literal `SOPS_AGE_KEY`** (no private key value in `docker inspect` / the env).
- **No keygen at build** (the control key is a host gesture, minted through a runtime `:rw` bind, never an image).
- **Operator identity is injected** (`KONTROLL_OPERATOR_{USER,UID,GID,HOME}`) so the root-in-container run chowns the
  canonical + renders `.env` to the real operator — a non-root `compose up` can then read it; the canonical isn't
  left root-owned (the F-CANON break). On a host-direct run those env vars are absent ⇒ byte-identical to before.

Covering check: [tests/unit/test_installer_phase1.py](tests/unit/test_installer_phase1.py) (named-volume
prohibition, ephemeral+portless, `SOPS_AGE_KEY_FILE`-not-literal, no build-time keygen, the `.dockerignore` guard,
the MF-1/MF-6 parameterizations) + the storage-chown guard's narrow installer-root exemption
([tests/check-storage-chown.py](tests/check-storage-chown.py)). The installer's blast radius **equals** today's
host-ansible (it does the same things, with a *narrower* dependency surface — a pinned image vs the unpinned host
toolchain that caused the fix(install) bug class). See [docker/installer/README.md](docker/installer/README.md).

**Phase-B extension — the CONTROL image is now a published CODE surface (the bake).** Phase B bakes the immutable
kontroll code + data registries (`api/ gui/ scripts/ ansible/` + the registries) into the control image at
`/opt/kontroll` (`chmod -R a-w`), so a baked deploy runs them from the image instead of a host `/repo` clone
(`docs/reviews/2026-06-29-phase-b-baked-code/`). This makes the **published, world-pullable** control image a
code-bearing surface — so the *same* no-secret-in-an-image floor above now governs it, with three added guards: **(M9)**
the build context must CONTAIN `api/ gui/ scripts/` (no silent un-bake) and EXCLUDE `instance/`, `local/`, the
age-key/secret patterns, and the `*.generated.*`-bearing config dirs (`prometheus/ dashboards/ config/`) — pinned by
`test_publish_images.py` against `.dockerignore`; **(M5)** the read-only-by-construction AST pin runs as a `gate` job
the publish `needs:`, so a tag can never publish code the pin hasn't cleared on that commit (baking moves the
read-only guarantee off the running clone onto the digest-pinned image); **(M6)** the baked importable package
`scripts/kontroll/` has a committed, per-file sha256 **recorded floor** (`docker/code-manifest.lock.yml`, the "L2b for
code" analogue of the image-digest byte floor + the L2b collection-tarball floor) — `gen-code-manifest.py --check`
runs in `validate` **and** the publish `gate` (so the world-pullable image's code can never silently drift from the
committed, reviewed manifest), and `gen-code-manifest.py --verify <tree>` re-checks a materialized baked tree
(`/opt/kontroll`) fail-closed for an audit. The age key / `instance/` are **never** in
the COPY set (the strip holds); the bake adds no secret to the layer. Covering check:
[tests/unit/test_code_manifest.py](tests/unit/test_code_manifest.py) (in-sync floor, no-omission coverage,
fail-closed on a changed/added file). The top-level promote slice is covered separately by **M8a** byte-identity
(`test_launch_kit.py`); the whole image by its sha256 digest (C0/C1).

### C18 — Passive discovery is a read-only, cred-reusing, untrusted-response surface  ◑ partial (Rungs 1a–3 + IPv6 + OpenWrt SSH + OUI hint built: read spine + GUI inbox + pre-fill + vendor DHCP siblings + the SNMP-ARP transport + dual-stack `neighbors_snmp` (v6) + the OpenWrt forced-command SSH lease read + the pinned IEEE OUI→vendor display-hint; live-dogfooded on a real FortiGate + a net-snmp responder)

> **IPv6 dual-stack read guard (SEE-ONLY, IPv6 rung).** The `neighbors_snmp` method walks the RFC-4293 dual-stack
> ipNetToPhysicalTable, so a v6 neighbour can now become a candidate. This widened the discovery read KEY only:
> `service/discovery._clean_ip` asks for a NEW dual-stack `ip` validator (`_v_ip` = `_v_ipv4` OR `_v_ipv6`, each
> keeping its config-injection + never-a-host rejections; `_v_ipv6` also drops link-local). The config-plane danger
> knobs (`mgmt_ip` / `ansible_host`, `type: ipv4`) are **untouched** — `_v_ipv4` is never widened, so onboard/reconfigure
> keep their v4-only guard. The read-only AST pin stays green (a wider read validator adds no write verb). **SEE-ONLY:**
> a v6 candidate surfaces + pre-fills `field-host`, but onboard validates `ansible_host` as v4 — so a v6 host is
> *surfaced for awareness*, not *onboardable*, until that knob is a deliberate, separate config-plane decision (its own
> `--check` + dogfood). Design-of-record: `docs/reviews/2026-07-03-discovery-corpus-growth/99-synthesis.md`.

(C17 is the **homelab** edition's control number — solo auto-promote, on the `homelab` branch; C18 is reserved for
this **edition-neutral** control so the two tines' control numbers never collide when `homelab` rebases onto `main`.)

The discovery inbox reads an already-onboarded device's OWN DHCP-lease / neighbour view read-only
(`scripts/kontroll-discover.py` → the git-ignored `local/discovery-inbox.generated.json` →
`service/discovery.read_inbox`), diffs it against onboarded inventory, and surfaces the un-onboarded delta so the
operator can onboard it — a human still turns every key (the Rung-1b pre-fill reuses the EXISTING
onboard→stage→promote; there is **no auto-onboard**). Bounded by construction:
- **Passive + read-only, never a scan (across ALL THREE transports).** The sweep's fetchers are EXACTLY three
  read-only device primitives: a hard-wired HTTP **GET** (the DHCP-lease methods), an SNMPv3 **`snmpbulkwalk`** (a
  read-only GETBULK of an L3 switch's ARP/neighbour view — the SNMP methods, Rung 3 + IPv6), and a **forced-command
  `ssh`** read (the OpenWrt DHCP-lease method — a byte-capped read of an AP's own dnsmasq lease file). None has a
  write/POST/SET/transfer primitive and there is no CIDR expansion, so each reaches ONLY an onboarded device's own
  address (`:161` for SNMP), never the network. The SNMP walk and the SSH read are **byte-bounded at the TRANSPORT**
  — a streamed, killed-on-overflow read of the child (the analogue of the HTTP socket cap), so a hostile/huge ARP
  table or lease file cannot OOM the control VM. The SSH read is **read-only by construction**: the client sends NO
  command word (the argv ends at `<user>@<host>`), so the AP's `authorized_keys` **forced command** (`cat
  /tmp/dhcp.leases`, host-key-pinned, `BatchMode`/no-TOFU) is the ONLY thing that can run. Machine-enforced by the
  `discovery-passive-only` grep-gate in `tests/validate.sh` — a NEGATIVE leg (no SNMP SET/raw socket/CIDR/POST) plus,
  for SSH, a case-SENSITIVE POSITIVE leg that forbids every write/tunnel/remote-exec construct (scp/sftp, the
  `Command`/`Forward` option families, ProxyJump/Tunnel/Subsystem, the `-F`/`Include=` config-indirection, and the
  `os.system`/`os.popen`/`pty`/`paramiko`/`os.exec*` alternate-spawn primitives) — plus the in-code twins
  `test_fetchers_expose_only_read_only_primitives` (the surface is EXACTLY `[http_get, snmp_walk, ssh_read]`, all
  read-only) and `test_ssh_read_is_forced_command_read_only` (an argv-AST proof of "no client command word after
  `user@host`", the property a grep can't see) + `test_actor_has_no_shell_true_and_no_extra_ssh_spawn`.
- **Zero new authority (the read-only pin stays green UNCHANGED).** The device reach + the artifact WRITE live in a
  TOP-LEVEL actor (`scripts/kontroll-discover.py`), OUTSIDE the pinned service layer (like `gen-validate-live.py`);
  `service/discovery.read_inbox` is a PURE read — no `WRITE_VERBS`, no write-open, no device reach —
  `assert_read_only`-pinned + covered by the fail-closed completeness gate. That the read-only AST pin is unchanged
  IS the proof this feature adds no promote/actuate path. (An `open(cache,'w')` inside `service/*.py` would trip the
  completeness gate — which is exactly why the sweep + its cache write are out-of-band.)
- **Credential reuse, as-scoped.** A credentialed source reuses the class's EXISTING read cred from its declared
  SOPS domain (the JSON DHCP methods → the `network` domain; the SNMP methods → the shared read-only `kontroll_ro`
  SNMPv3 user of the `snmp_observability` domain, the same monitoring GET cred the snmp_exporter already uses; the
  OpenWrt SSH lease method → a **DEDICATED forced-command** read key in the `network` domain
  (`openwrt_discovery_ssh_private_key`), **never** the full-shell Ansible root key), never widened; the value is only
  an `${ENV}` ref resolved at the fetcher boundary. The operator EXPORTS `KONTROLL_SNMP_V3_*` (decrypted from
  `snmp_observability`) or, for OpenWrt, decrypts the read key to an ephemeral `0600` file and EXPORTS its PATH +
  the operator-pinned known_hosts PATH (`KONTROLL_OPENWRT_DISCOVERY_KEYFILE`/`_KNOWN_HOSTS`) before the on-demand
  sweep — NOT deploy-provisioned into a `.env` (exactly as for `gen-validate-live`); unset ⇒ the source is
  `no-creds`, fail-closed, never an unauthenticated/TOFU fallback. The audit line + the artifact carry the class/method/status/count NAMES only — never a
  credential, never a discovered identifier (C2/C12). Residual: net-snmp's `-A`/`-X` put the v3 passphrases on the
  child argv (visible to a local `ps`/`/proc` reader for the walk's lifetime) — accepted under **C3** (single-operator
  mgmt VLAN); a raised error is cred-free (`rc=<n>` only, stderr discarded).
- **A discovered lease is UNTRUSTED DATA, not code.** Byte-capped at the socket + row-count-capped at parse (the
  flood cap) before it is written; every field is re-validated on the pure READ side through the SAME closed
  `_validate` guards the config plane uses (a bad IP dropped, a bad hostname/MAC → None), so no un-normalized device
  byte reaches the service result. The GUI (Rung 1b) additionally renders every device byte via `ET`/`textContent`
  — a lease is attacker-influenceable, so a `<script>` hostname renders as literal text (F6, e2e-proven) — defense
  in depth, not the boundary. The `GET /api/discovery` route is a thin pure passthrough of `read_inbox` (mirroring
  `/api/pending`); the inbox's "Onboard this" only PRE-FILLS the discovered host into the EXISTING onboard form (the
  human picks the class + fills creds + Runs — discovery reaches no onboard/stage/promote path of its own).
- **The OUI vendor hint is OUR committed data + a DISPLAY HINT, never a class guess (F8).** The candidate's `vendor`
  is looked up (pure, offline, longest-prefix) from the pinned IEEE OUI registry (`oui/`, derive→pin→`--check`,
  gen-oui.py; the network `--refresh` is operator-only, grep-gated out of every install path — BRICK-1) keyed by the
  already-`_clean_mac`-validated MAC — so it is not a device byte, adds NO device reach and NO new authority (the
  read-only AST pin stays green; `_load_oui`/`_vendor_for` are pure reads). It is rendered `textContent` and read by
  NO code that selects a class: it aids the human's class-pick, it never makes it (an e2e asserts "Onboard this"
  pre-fills only the host, no class pre-picked). A MAC's OUI is a *manufacturer*, not an Ansible *collection* — the
  class-guess F8 deliberately cut stays cut.
- **No live identifiers in committed data (P-1).** The runtime artifact is git-ignored under `local/`; committed
  fixtures/goldens use documentation identifiers only (RFC-5737 IPs, RFC-7042 MACs), enforced by the
  `discovery-p1-fixtures` grep-gate.
- **Tier: control-VM / on-demand, never hermetic CI or an install-gating path** (it reaches the lab — the BRICK-1
  discipline; deliberately absent from `validate`/bootstrap). Its dispatch/parse is proven hermetically via an
  injected fake fetcher.

**Proves:** `tests/unit/test_discovery.py` (the declared offer reaches opnsense while a `{plugin: httpapi}` facts
predicate would not; the fetcher is GET-only; a hostile body is tolerated; the IP-diff hides an onboarded host; the
flood cap bounds rows; hostile fields are dropped/normalized; `read_inbox` is `assert_read_only`-green) + the
`discovery-passive-only` + `discovery-p1-fixtures` grep-gates in `tests/validate.sh`; the `/api/discovery` route
(`tests/integration/test_gui_api.py`: auth-gated 401 / pure passthrough / degrade-never-crash 500) + the GUI e2e
(`tests/e2e/test_discovery_flow.py`: the panel renders, a `<script>` hostname renders as literal text and never
executes, the "Onboard this" pre-fill lands the discovered host in `field-host` with `apply` unchecked).
Design-of-record: `docs/reviews/2026-07-03-discovery-inbox-scope/99-synthesis.md`. Accepted-risk row **R-DISC-1**.

### C19 — The GUI renders foreign bytes as TEXT, never markup  ✓ implemented (source-gated, allow-list)

The GUI paints values it did not author: **device** bytes (capture filenames and line bodies — C14; lease/ARP rows
— C18), **third-party publisher** bytes (`scripts/kontroll/probe.py` copies a collection's `description` verbatim
out of the public Ansible Galaxy API and `service_search` returns it to the browser), the bytes of a **staged,
unpromoted proposal** (the Pending panel lists a `proposed/*` ref's name, its commit subject and its changed path
names — and, apart, any ref beside the proposals that the service did not stage), **repository** bytes that a
promote put on `main` (inventory, fleet, descriptors, settings), and every
route's **error string**, which can quote request input, a repository value or a remote response.

**The rule is an allow-list: only literal markup written in `index.html` reaches `innerHTML`.** The template's DOM
helpers are `E()` (assigns `innerHTML`), `ET()` (assigns `textContent`), `T()` (a text node) and `LINK()` (an
anchor). `E()`'s markup argument and every direct `.innerHTML =` is a concatenation of string literals; anything
that is not a literal is painted with `ET()`/`T()`, or an empty element is left in the literal and its `textContent`
filled. There is no escaping helper and no judgement about which values are trustworthy: a value's source does not
enter into it. Two neighbours that text-painting does not cover are closed the same way. A link's `href` is assigned
in exactly one place, `LINK()`, and only when `safeHref()` — which asks the browser's own URL parser — resolves the
value to http(s): a `javascript:` URL in an `href` runs on click however its label is painted, so a value that is
not a web link is shown as text. And a dynamic segment of a same-origin request path goes through
`encodeURIComponent`: unencoded, a secret domain of `../keygen/control` made the secret dialog's Save reach the
route that mints a key.

History, because the rule was reached twice. Until 2026-07-27 the search card painted the Galaxy-authored
`description`, `collection`, `version` and `origin` through `E()`: publishing a collection whose `galaxy.yml`
description is `<img src=x onerror=…>` gave script execution in the operator's authenticated session **the moment
they searched**, with that session able to reach every `POST` route the GUI exposes. That fix shipped with a gate
that was a **denylist** — it searched `E()` arguments for seven substrings (`rec.`, `data.error`, …) — and the
denylist was the next hole. A review on 2026-10-11 found the Pending panel painting `p.run_id`, `p.subject` and
`p.paths` as markup: names the list did not contain, carrying bytes that exist **before any human has promoted
anything** (a ref name may contain `<`, `>` and `=`, and nothing validates a run id before it is listed). The same
census found fifty-nine sinks painting a non-literal, none of which the gate could see — among them the error line
of every panel not yet converted, written with a direct `.innerHTML =` the gate never read, and two `href`s built
from server strings. All were rewritten on 2026-10-12, and the gate became the allow-list above.

Defence in depth: an `after_request` hook sets `Content-Security-Policy: object-src 'none'; base-uri 'none';
frame-ancestors 'none'` plus `X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin`. **`script-src`
is deliberately absent** — `index.html` carries one large inline `<script>` and inline `onclick` attributes on its
header buttons, so a `script-src` policy needs a per-response nonce and those handlers rebound first; that is a
named follow-up and is *not* a substitute for the paint. The paint is the control; CSP is the second layer.

Covering checks: [tests/unit/test_card_paint_gate.py](tests/unit/test_card_paint_gate.py) — a source scan of the
template that understands string literals, comments and regular-expression literals. It fails on an `E()` markup
argument or an `.innerHTML` right-hand side that is not literal-only; on the sink under another spelling (an alias
of the helper, a literal bracket key, an interpolated template literal, another HTML-parsing or string-executing
API); on an `href` outside `LINK()`; on an executing attribute or a navigating property set from script, and on an
attribute or property whose name is not spelled in the source; on an element created from anything but one inert
literal tag name; on a request that is not a direct `fetch()`, and on a request path that carries anything
unencoded except the few names pinned to the one place their value comes from; and on code the script scan does not
read (a second script element, a handler attribute that is not a bare call, a template expression in the static
markup or one in the script that is not `| tojson`, a hidden line-break or display-reordering character). It proves
it can fail by planting each shape a review found into the real template, and it pins the helpers by their exact
definitions. It is a scan over tokens, not a JavaScript parser. A string or a regular expression that does not
close is refused; a span it misreads as a string or a regex, and a property reached through a computed name
(`x[k] = v`), are beyond it. Those are shapes written on purpose — the gate stops the sink coming back through an
ordinary edit, and the paint and the browser proof are what answer the rest.
[tests/e2e/test_paint_rule.py](tests/e2e/test_paint_rule.py) is the browser proof for the Pending, Index,
Settings, Homepage and Discovery panels and the capability, reconfigure, secret and keygen dialogs: each is served
a payload in the fields it paints and must show it as text, create no element and run nothing. There is no list to
extend — a new value on the page is either a literal or painted as text.


### C21 — The shippable tree carries no identifier of a real deployment  ✔ implemented (2026-10-08, the public split)

**Threat:** the tool is developed against a real homelab; an address, hostname, domain, MAC, key
or operator path pasted into a doc, a fixture or a CHANGELOG entry describes that lab's attack
surface to anyone who reads the public repository — and the earlier guards *embedded the very names
they blocked*, so publishing them would have published the denylist.
**Implements:** `tests/_leak_guard.py`, one scanner run from three places — `tests/validate.sh` step
`pii-guard`, the required check *No leaked personal identifiers* (`.github/workflows/pii-guard.yml`),
and the pytest twin `tests/unit/test_leak_guard.py`. Layer 1 is **structural** (public, always on):
any RFC-1918 address in dotted/dashed/underscored form, a non-documentation MAC or global IPv6, a
real-length age recipient / `AGE-SECRET-KEY`, the maintainer's personal e-mail, an operator-machine
user-profile path; RFC-5737 TEST-NET, RFC-7042 MACs and `2001:db8::/32` are the sanctioned examples.
Layer 2 is the **instance-token list** — this deployment's hostnames/domain/users/subnets/VLANs as
regexes in `instance/leak-tokens.txt` (private overlay; the public CI gets the same list as the
repository secret `KONTROLL_LEAK_TOKENS`; a public checkout runs the shipped canaries in
`instance.example/leak-tokens.example.txt`), reported by index + digest only. The scope is every
git-tracked file minus the private strip set (`instance/`, `local/`, and `docs/reviews/` only on an
instance tree — a public tree scans its dossiers); a line that must hold a private address carries
`pii-guard: allow <reason>`. The same `--tree` run checks the **overlay invariant**: a PUBLIC tree
tracks nothing under `instance/` and ignores its entries (`/instance/*` in the root `.gitignore`); an
INSTANCE tree tracks its overlay and every tracked path is re-included by `instance/.gitignore`
(scaffolded from `instance.example/.gitignore`), so a `git add -f`, a lost rule or a missing re-include
file fails with the paths — checked with `check-ignore --no-index`, since a tracked path is never
reported as ignored otherwise. The converse — an overlay entry the tool WRITES that the scaffolded file
does not re-include, so that `git add` refuses it — is held by `tests/unit/test_staged_write_lands.py`,
which reads the entries out of `paths._OVERLAY_MAP` and out of the example instance. `kontroll.paths.resolve()` reads
the shipped `instance.example/` on a checkout with no `instance/`, so the public tree passes its own
gates with no overlay present; writers (`keygen`, `enable_in_fleet`, `kontroll-init`) pass
`write=True` and can never target the example.
**Proves:** `tests/unit/test_leak_guard.py` (dashed/dotted/underscored RFC-1918 caught; TEST-NET,
OIDs and version strings never; a planted canary fires and is reported without its value; the whole
shippable tree scans clean), `tests/unit/test_paths_overlay.py` (the example tier is read-only and
off once `instance/` exists), the PII-guard workflow's path assertion (no tracked `instance/` path in
a tree whose `.gitignore` excludes the overlay). Design-of-record and the full sweep:
`docs/reviews/2026-10-08-public-split-pii-sweep/`; operator runbook `docs/public-split.md`.

### C22 — Request fields are closed charsets; drop-in writes are confined  ✔ implemented (2026-10-09; leftovers closed 2026-10-12)

**Threat:** the onboard/reconfigure/actuation requests carry fields that become PATH COMPONENTS
(`modules/<key>/module.yml`, `onboarded-<key>.yml`, `ansible/secrets/<domain>.sops.yml`,
`actuation/<key>/unit.yml`) or INVENTORY KEYS (a host name, an address) — and until 2026-10-09 the planner
validated only the collection's installed-ness. `key`, `group`, `host`, `host_name` and `secrets` were bare
strings joined into paths and rendered into YAML (2026-10-08 review, finding 1; CodeQL `py/path-injection`,
45 alerts on the same class of join). A second pass on 2026-10-12 found the boundary still open in several places:
a collection name reached `ansible-doc`'s command line unvalidated — on routes that need no token; the
validators accepted a value followed by one newline, `hostname()` accepted an IPv6 zone id with any text after
the `%`, and a name such as `null` or `123` passed the charset although YAML reads it as another type once it
stands unquoted in a file; the telemetry, logging and backup plan builders joined `key` into a path before
anything had checked it; and a telemetry `dashboards` selection was written into the class's module file by
string formatting, where one name with a newline rewrote the class's role and inventory group, and one with none
rewrote the item itself.
**Implements:** `kontroll.paths.component()` (a device-class key, unit key, secret domain or inventory group:
`[a-z0-9][a-z0-9_-]{0,63}`), `paths.collection_fqcn()` (exactly `namespace.name`), `paths.hostname()` (an
IP literal **without a zone id**, or an RFC-1123 hostname) — each a pattern that ends in `\Z` and is applied
with `fullmatch()`, because `$` also matches before a final newline — called at the service seam
(`build_onboard_plan`, `build_scaffold`, and first in the telemetry, logging and backup plan builders, before
anything is read, probed or written) AND at every join (`paths.module_file`, the inventory drop-in path, the
secret-domain path, the unit path, the probe's `ansible_collections/<ns>/<name>` walk and the `ansible-doc`
argv word in `probe.deep_probe`), so a value that passed the seam is re-checked where it is used and a new
caller cannot forget. A charset is not the whole rule for a value that is written into YAML by hand (to keep a
file's comments, or because a generated file is rendered line by line). `kontroll/yamlscalar.plain_scalar()`
refuses a word that PyYAML — the reader of every file these writers touch — reads as another type (`null`,
`true`, `123`, a date), and a word that is structure **where a writer puts it**: after `key: `, as a list item,
before a trailing comment, and, when the caller renders into one, inside a flow mapping. A string with a
line-break character is refused outright — the Unicode separators included: PyYAML keeps those verbatim in a
bare scalar, so a check of the bare word alone would pass them, and the file would have a line more. The rule is
applied to
every onboard field that becomes a NAME (the class key, the inventory group, the secrets domain, the backend),
a dashboard name, a backup remote name and every `text` and `hostname` knob, and again inside the writers
themselves: the scalar writers of `service/_blockwrite.py` take None, a bool, a number or such a string and
nothing else, `onboard.enable_in_fleet` re-checks its key, and the settings and identity planners answer a
writer's refusal as `bad_value`. A HOST name is not held to it (`123` and `true` are host names): the inventory
is serialized, and a generator that renders a name it merely read back — a group or a host into the Prometheus
targets, a group or a class key into the backup schedules, a unit's group and collection into the play preview,
the instance keyring into the trust sidecar — writes it through `yamlscalar.word()`, bare when it is a plain
word and double-quoted when it is not. No NAME a generator **renders** is refused at deploy time — it quotes what it
cannot write bare — and no shipped
generated file changes by a byte. A fleet line is first of all a selector: it names a directory, and there is no
quoted form of a path. Every Python reader that resolves a class through a path takes its class keys from
`paths.enabled_keys` (the Settings view, which lists the fleet as it is, apart — the known gaps below), and
`bootstrap.yml`, which loops over the list itself, runs the same rule as a check first and stops on it; the
services, the checks and the generators that render skip a line that is not a key, and the generators say which
entry — its place among the list's entries, counted from one; comment and blank lines are not counted
(`paths.fleet_problems`: the place and the rule, never the entry — what an operator reads in each seat, and what
to do, is [docs/troubleshooting.md](docs/troubleshooting.md) §6); `gen-requirements.py` — the reader that decides
what is INSTALLED — refuses the fleet, at the promote gate too. Of a NAME or a fleet line the deploy-time
refusals are therefore these and no others: `gen-requirements.py` on a fleet line that is not a class key and on
a collection entry or a trust statement it could not render as itself — below — and `gen-homepage.py` on a tile
field that holds a line break or a Homepage substitution marker, which Homepage replaces in the raw text before
any parser runs. No value a request can store meets any of them: the one request writer of the fleet list holds
its key to the same rule (`onboard.enable_in_fleet`). The generators also fail closed on a module file or a
registry entry whose SHAPE they cannot render — a method the registry does not hold, a parameter outside its
allow-list, two entries for one job — and `gen-requirements.py` on two units that pin one collection differently
and on a fleet-wide `signature_policy` outside the closed set; and on a fleet FILE that holds no list of classes
— which no renderer renders either, and over which a capability change is refused — or that does not parse:
refusals of structure, which this control does not list.
(`gen-homepage.py` also refused any name containing the word `password`, which a request CAN store; it no
longer does, and an exhaustive test over the request charsets holds that.) The check ANSWERS every
string: a word PyYAML resolves to a type and then cannot build — a date that does not exist, a hex number with no
digits — is "not plain", never an exception, because the same check runs inside those generators and inside the
validator that the discovery inbox applies to names a DHCP client chose. (A YAML 1.2 reader differs from PyYAML
on a few words — `1e3`, `0o17`; the one non-Python reader of a file these helpers write into is Prometheus, whose
labels are strings whatever the scalar looks like, except `null`. The Homepage tiles, read by a JavaScript YAML
parser, are quoted field by field by `gen-homepage.py` itself.)
A telemetry `dashboards` selection is a closed shape — a bounded list of exactly
`{gnet: <positive integer>, name: <one path component>}` — and a name is one file and a board is one name
(`observe._clean_dashboards`): no name repeated, none that any class already declares with another board id, no
board id given a second name, and none that is already a file under the dashboards directory which does not say
it is that board (`gnetId`) — the hand-authored logs dashboard is such a file, and no class declares it. The
name is re-checked where it becomes a file name: the uid read in `observe._dashboard_uid`, and
`scripts/fetch-dashboards.py`, which fails closed on a module file whose `dashboards:` is not a list of items
with a positive-integer `gnet` and a one-component `name` (other keys, such as `derived`, are allowed there), on
one name declared with two board ids, and — before anything is fetched — on a target file that is not the board
being fetched; a board it writes says which board it is. A unit's install pin is `==` plus one plain version
word (`_actuation_schema.PIN_VERSION_RE`) and its digest sixty-four hex digits; for the kinds that install
something, the collection it runs is a collection name and what it runs there is one word of at most sixty-four
characters (`validate_unit`: `paths.is_collection_fqcn`, `UNIT_NAME_RE` — the rules the create route applies to its
own inputs, now the descriptor's, so a hand-written `unit.yml` is held to them wherever the validator runs: in
`gen-actuation.py`, which the validate gate calls, and as the create route's last check; the limits below say
where it does not); and `scripts/gen-requirements.py`
refuses, offline — also at the promote-time gate, which resolves without rendering — a collection entry whose
name is not `namespace.name` with each half starting with a letter or whose version holds anything but version
characters, and a unit's trust statement whose signature policy is outside the closed set or whose digest is
not one: the policy stands unquoted in the trust sidecar, where one with line breaks added an entry for another
collection. A literal host var from a backend's or a class's `inline` credential field is TYPED before it is
written, because Ansible templates inventory variables and the request must not choose the bytes (S28): a `port` is
an int in 1..65535 parsed from one to five ASCII digits (`re.fullmatch`, so a trailing newline or another script's
digits are not one), a `bool` is True or False from exactly `true` / `false` (`authspec.inline_value`, refused as the
field otherwise, 422 `invalid_<field>`); no other kind may be inline, and a port lands only in a `*_port` var
(`authspec.inline_target`, refused as repository data otherwise). The first such field shipped is the SSH backends'
`port` → `ansible_port`, which `ping.yml`'s probe templates on the control node; a blank value is no value. A
backend's `probe_port`, written to every host of that backend as `kontroll_probe_port`, is a YAML integer port or
every plan on the backend is refused (`tests/unit/test_onboard_port.py`; the request half also by the route rows
in `tests/integration/test_api_onboard.py` and `test_gui_api.py`). And a credential field's stem
is an identifier before it is formatted into the host var's lookup expression — a field the backend registry or a
class's module file declares with another name is refused once a value is given for it (as repository data, below),
and an option of an installed module whose name is not an identifier is not offered as a field at all (`authspec`: a
third party's spelling never refuses an onboard; where such an option is required or marked `no_log`, the module's
set is not exact and is not used). Its `maps_to` — the NAME of the host var a value is written under — is an
identifier too, wherever a value is written under it, and an `inline` field must name one (which host var is the
declaring file's choice; see the limits below). A `maps_to` holding a `{placeholder}` is a TEMPLATE (S27: the api
backend's and the generic union's `{cred_key}_api_token`, the var the former branch on the field's name wrote): it is
rendered with the host's credential key alone — `catalog.render_template`'s closed `{name}` grammar, no attribute
access, no Jinja — and its result is held to the charset of the host names the request boundary accepts
(`catalog.MAPS_TO_RE`), not to an identifier, so a dotted or IP-literal host keeps the var it had — names
ansible-core 2.19 accepts with a warning until 2.23 (any var name that is not an identifier: a `.` or a `:`, or a
leading digit, as every IPv4 literal has), so they must become identifiers before the stack reaches it; a template
that does not render is the declaring file's, refused as repository data (`tests/unit/test_backend_defaults.py`).
Only the backend registry and the generic union declare a template: it lets the request's host name choose the var,
so a module file's `auth:` entry may only repeat, for the same field, the template its backend's registry entry or
the generic union declares (today only `api_token`'s `{cred_key}_api_token`); any other template is refused as
repository data once a value is given. The
backend registry's `default_secrets_domain`, which stands in for a domain the request omits, is held to the rule of
a name as repository data before the plan uses it. The module-reuse path ADOPTS a class's group, secrets domain,
backend and role from the module file already on disk; each one the plan takes is held, where it is taken and before
anything is written, to the rule of the request field of the same meaning — for the role, which no request carries,
the rule of a name (`onboard._adopted`): the adopted domain is formatted into that same lookup expression, the role
is a host var Ansible templates, the group is the drop-in's top-level key. A module file that fails — or that is not
a mapping at all — is not a request refusal (below). The role a class gets when its module file names none is the
backend registry entry's own, never one made from a name (S26: the planner and the scaffold made `backend_<name>` up
for a backend that declared none, and wrote it into the module file and every host's `device_role`, where
`ping.yml` and `backup-configs.yml` then included a role that does not exist). A `backend` a request names must be
one this install ships (`onboard._registered`) whose entry declares a `role:` (`onboard._role_of`) — a reuse
(an onboard of a collection the class under that key already declares, or, under a class with `collections: []`, a
collection ansible-core ships) whose module file declares its own role keeps that role, so there the entry needs none;
any other is a refusal of the `backend` field, before the collection is probed. A backend the classifier matches is
chosen only when its entry declares one (`catalog.runnable`) — for a new class; a reused class with its own role falls
back to the first match when none declares one — and a search record's `suggested_backend`, which the GUI's onboard form
sends, is that same choice. The registry role is held to the name rule where it is taken, by the planner and by the
scaffold; one that fails is repository data, and so is a reused class whose module file names no role and whose backend
— from the file — declares none, or names the role the planner used to make up for a backend that declares none
(`backend_vendor_config`, `backend_napalm`; `onboard._no_made_up_role`, which the identity knob also refuses to
store): what one request writes into a module file, the next one adopts. Each of these faults of a reused class's
file is answered before the collection is probed. A backend the file names that this install does not ship is not
one the planner checks: a class that brings its own role plans with it, and no connection shape. An
inventory group a class is ATTACHED to — the onboard field, the identity knob, the scaffold's argument, a group
adopted from a module file — is additionally none of `all`, `ungrouped`, `localhost` (`paths.inventory_group()`): a
class's group is a play's pattern — `hosts:` in the logging wiring play and a unit's play, the limit of the backup
schedule — where `all` addresses every host; and a group named `localhost` captures every play written for the
control node (with such a group in the inventory, `hosts: localhost` addresses that group's hosts — measured). The
NAME a request gives a host in the inventory is none of these either, nor `127.0.0.1` or `::1`, which are the same
host to Ansible (`paths.inventory_host()`; the address is not a name of the inventory and is not held to this): an
inventory host that carries one of Ansible's names for the control node replaces the implicit one, so every play
written for the control node runs against it, and with a host named `all` the pattern `all` addresses that one host
(each measured). A group merely read back is never refused by a generator or a play, and the identity dialog does
not judge a value it reads back unchanged — only one that is chosen (a value the file holds as something other than
a string — a number, a boolean — is read back as text and is judged). The identity knobs that hold a name are
bounded in length like the join their value ends up in (sixty-four characters); their charset is narrower than the
onboard field's (the limits below).
A write of a drop-in file — a module file (at onboard, at each reconfigure and from the scaffold command), the
inventory drop-in at onboard, a unit descriptor and its values — goes through `paths.confined()`, which resolves
the target under `write_root()` and refuses an absolute path, a `..` that climbs out, a NUL or a symlink that
points out (so in an `instance/` that is a symlink out of the write root the drop-in files — an inventory
drop-in, a unit descriptor and its values — are refused, and the refusal says so; the files written at a fixed
path, listed next, are written through it). No other write a request reaches passes it. Written at a
fixed path: the fleet file, the instance file, the capture-exception file, the recipients file (`.sops.yaml`), the
Homepage board's `services.yaml` and `settings.yaml`, the generated backup-schedule and Loki-retention files, and
the git-ignored matrix cache. Written at a path whose one variable part is a name `paths.component()` has
checked: a host-vars edit and a secret domain. Written at a path whose variable parts are a registry's own word and
a class key: the Prometheus target files and
the Vector fragments that a telemetry or logging change regenerates inside the worker (and whose orphans it
deletes) — each is named after a method of the registry (the telemetry method's `job`, the logging method's
`name`: repository data, held to no charset where the path is built) and a class key of the fleet file
(`paths.enabled_keys`: a line that is not a key never reaches the join). The audit logs are written where the
deployment's environment points them. The instance-overlay existence probe applies `confined()`'s rule to its
own join, for a READ (`paths._overlay_file_present`: a mapped path that climbs out is "absent"; a symlinked
`instance/` is followed, on purpose). Every Python reader that RESOLVES a class through a path takes its keys from
`paths.enabled_keys` /
`paths.module_keys` — the Settings view apart — so an entry that is not a class key is never joined into a path
by a service or a generator; `bootstrap.yml` loops over the list itself and is held by the check below. The
service readers (classify, the Fleet index, the audit, the discovery sweep), the checks and the generators that
RENDER (targets, drop-ins, tiles, schedules, jobs, exporters, the secret-env manifest) skip such an entry — it is
data read back from a file, not a request field, and some of those generators run inside the API and the GUI,
where stopping would end a worker after a module file was written — and each generator says on stderr which
entry it skipped (`fetch-dashboards.py` and the discovery sweep skip without a line). A file that holds no list
at all — it is not a mapping, or `enabled_modules` is not a list — is not an empty fleet to a renderer: it
renders nothing and prunes nothing, says so on stderr and returns non-zero. A deploy does not get that far (the
check below stops it first); a capability change made through the API or the GUI is refused before it writes
anything (`_blockwrite.require_fleet_list`: 409 `repository_data`, with its audit line), because the regeneration
that would follow it would render nothing. The checks (`gen-class-capabilities.py`, `gen-validate-live.py`) and
the service readers still read such a file as a fleet with no class, the checks with an `ignoring:` line. The one that decides what is installed does not skip:
`gen-requirements.py` refuses a fleet whose `enabled_modules` is there and is not a list of class keys (an absent
or empty list is a fleet with no class) — at bootstrap, at a deploy that builds its images locally and brings up
the runner, the API or the GUI, and at the promote gate, where the refusal keeps the line out of `main` for every
promote that is not run with `--skip-conflict-check` — within that gate's own limit (C10: the fast-forward resolves the
proposal's name again). The gate judges the whole prospective tree: with such a
line already on `main` (a hand edit) it refuses every promote except the one that removes the line. The plays run
that generator's `--fleet-check` before anything walks the list — one rule, in Python, not a copy of the charset
in Jinja — and each STOPS on it: `bootstrap.yml` before its own lookup; a deploy in `local-canonical.yml`, before
the blanket commit and the mirror that take the operator's worktree into the canonical's `main` with no promote
and so no promote gate — which is where an ordinary deploy stops — and again in `deploy-stack.yml`, before the
first generator rewrites a file the running stack reads, for a run that starts later in the play. That a deploy
stops is the careful default, not a decision the operator has taken: one that only warned would drop the class's
targets, drop-ins and tiles, would put a hand-edited line on `main` — on every node whose overlay git tracks,
which `kontroll-init --fresh` sees to — where it refuses every promote, and on a node that builds its images
would stop anyway, at the collection lockfile, after those files were rewritten.
A refusal is a `RequestBoundaryError` whose message names the field and the rule and **never the value**. It is
mapped **once per app** — `api/main.py` and `gui/app.py` both answer **422** with a `code` derived from the
field, before any write, so a route with no `try`, or a new one, cannot turn it into a 500 (the two onboard
routes catch the planner's other guards as well and build the same body themselves). A privileged refusal is
**audited by field name**, never by value — the value can itself be the payload: `onboard-refuse` on the
onboard routes (`reason=request-boundary field=<name>`; a planner guard that is not a field refusal says
`reason=planner-guard`), `request-refuse` with the route elsewhere — the path TEMPLATE on the API, the endpoint
name on the GUI, because a path parameter is a request value too. An open inquiry route has no principal and
writes no audit line, so a refusal gives an unauthenticated caller no new way to write the audit log (its only
lines stay the auth-denial and the throttled rate-limit line). Some routes audit request text BEFORE
validating it (a class key, a method name, a free-text subject); every field of every audit line goes through
one flattener (`kontroll/auditfmt.py`, shared by the API's writer and the GUI's), so a tab or a line break in
such a value cannot add a column or end the row early — a lone carriage return used to do the latter and hide
the rest of the entry from the audit query. The Fleet index paints host names and addresses as text (the C19
rule applied to inventory bytes).
A refusal that is not about anything the caller sent — a module file whose adopted name is not a name, or that is
not a mapping (at the onboard and at an identity edit; the other readers of a module file — the capability dialogs,
the Fleet index, classify, the cred-fields route — still answer 500 for such a file), a credential field that the
backend registry or a module file declares with a name that is not one, a class whose module file names no role and
whose backend declares none (or whose backend's registry entry holds a role that is not a name), a write target that
does not resolve under the write root, a block of a module file that the block writer cannot edit where it stands
(its value on the key's own line, the key held twice, or a block whose edit would not read back from YAML as the
change that was planned) — is a `RepositoryDataError`, deliberately NOT a `ValueError`, so a handler written for
the planner's guards cannot answer it as a refused field. It is mapped once per app to **409** `repository_data`,
with a sentence that says where to look — the class and its module file with the rule the value breaks, the class
and the backend whose credential field it is, the class whose backend needs a `role:` or whose registry entry holds
one to correct, or the layout — and never the value, and audited under an action of its own, `repository-refuse
what=<kind> route=<route>` — never as a refused request (on the API an open route has no principal and writes no
line, as for a field refusal; every GUI route is authenticated and writes one). `apply_onboard_plan` confines the
drop-in targets it is going to write — the module file and the inventory drop-in of a new class, the inventory
drop-in alone for a reuse (an onboard of a collection the class under that key already declares, or, under a class with
`collections: []`, a collection ansible-core ships) — before its first write, so a drop-in the confinement refuses stops
the apply with nothing written; the fixed-path writes that follow (the fleet file, a secret domain) are not confined;
and nothing an apply has written — drop-in or fixed path — is rolled back when a later step fails. `POST /onboard`
therefore has more than one 409, told apart by `code`: this one, and a drop-in that exists with different content (no
`code`; the caller may retry with `overwrite`) — which, on an install whose write root is not its code root, includes a
class the tool SHIPS under the same key for another collection: the apply asks the reader's view before its first write,
because a module written there would shadow the shipped class for every reader. **Known gaps, each with a theme of its
own:** `gen-homepage.py` quotes a host name with its own escaping, which knows the newline and the carriage return and
not the other line breaks, and `gen-logging.py` formats a host name and a class key into a string literal by hand — and
nothing compiles the fragments that generator writes before Vector loads them: the gate checks them for freshness, and
its `vector validate` reads the hand-written configuration only; a few other anchored patterns (the capture revision,
the discovery tokens, several generators) are still applied with `match()`; the Settings view lists the fleet as it is —
it shows some malformed lines and cannot remove one that is not a string; a
tree that ALREADY gives one board two names is fetched under the first only (a request can no longer create that);
and a UNIT may still target `all`, `ungrouped` or `localhost` (the class seams above refuse the names; the unit's
validator does not look), or no group at all, which its play renders as `all`.
**Limits written down here, not closed by this control's follow-up:** a fleet line that IS a class key and has no
class directory is skipped by every generator without a word (`bootstrap.yml`'s own lookup stops on it; a deploy and
the promote gate do not: on an install whose code root is
not its write root a class file has two homes — a reader looks in the write root and then in the code root,
`paths.module_file` — and a generator looks under one root only, so "no directory here" is not a fault it can
tell). An EMPTY fleet file — no bytes, comments only, `~`, `[]` — is a fleet with no class to every reader: it
passes the check and the promote gate, and every renderer then prunes what it generated, without a word ("a file
with no list" above stops one shape short of a truncated file; whether an absent `enabled_modules` key should
count as no list is a rule of its own, not built). The plays' check is the fleet LIST's rule and no more: a
fleet-wide `signature_policy` outside the closed set, or two units that pin one collection differently, pass it
and are committed — and are then **refused by the landing** that follows the commit (every way into the
canonical's `main` passes the promote gate since 2026-10-19 — C10, "Every way into `main` lands": the deploy's
mirror task, `scripts/update.sh`, `scripts/update-from-bundle.sh` and the operator CLI's onboard each judge the
commit before anything touches the canonical, and a refusal says what to correct), so such a line stays in the
checkout, one commit ahead of a canonical that did not take it, until it is corrected there; on the launch kit the
launcher commits the kit tree — a hand-edited line included — into the kit's own repository before the play refuses
to land it. A line
that reaches `main` one of those ways is met by the next deploy's check and by every promote. A lookup that finds nothing still quotes the
unvalidated value it was asked for back to its sender (it belongs with the work on error bodies, which rewrites
those answers). The Tier-cap compares group NAMES: a parent group an instance defines over `edge_firewall` or
`core_switch` is neither name, and the play of a unit attached to it addresses them (C15-a, where the limit is
written down with what would close it). In a hand-written descriptor a unit's `collection` need not be one that its
`install.collections` pins, and a standalone unit's playbook path has no charset (both are C15-a's to close).
Nor is the descriptor's validator on the path a descriptor is READ by: the readers of the unit registry
(`catalog.load_actuation_units`) take any file that parses and has a key, so a unit nobody ran `gen-actuation.py`
over reaches the preview and the registry's list as it stands — the preview therefore still writes every field it
reads through `yamlscalar.word()`, and the API's preview route answers 500 for a name that is not a string. A
`standalone` unit is not asked about a `collection` or a `name`, which it does not need: one that carries them
anyway is valid, and the preview's header names them as what runs. And a unit's key is bounded in length only
where the create route derives it; a directory made by hand with a longer name is a unit like any other.
`kontroll-init --fresh` writes `--mgmt-ip` and `--domain` as given into the instance file and into the Homepage
board's `services.yaml` — at a shell, by the operator, into their own files. (`scripts/kontroll-promote.py`
holds its `run_id` to the two minted shapes itself, whoever runs it; the promote play checks the same rule before
it calls the script.) A credential
field's `maps_to` is held to being a variable name (or, for a template the backend registry or the generic union
declares, to `catalog.MAPS_TO_RE`), not to a set of variables: a module file's own `auth:` entry stored in SOPS may
write its lookup under `ansible_connection`, `ansible_host` or any other host var of that class's hosts, and an `inline`
flag may write `true` or `false` under any lower-case one (only an inline port is held to a `*_port` var) — a file an
operator promotes, never a request. The request's text reaches a var's NAME only through a template the backend
registry or the generic union declares (shipped: `{cred_key}_api_token`, whose rendered name ends in `_api_token`);
the planner holds an operator's registry template to no suffix — the registry is operator data, trusted as the role
it names is. The class seams refuse
the names a NAME can tell; a PARENT group the inventory defines (the example's `network_devices`, `compute`) is a
group like any other to them, and a class attached to one addresses its children wherever the class's group is a
pattern. The identity dialog's charset for a
NEW value is narrower than the onboard field's — no hyphen, no leading digit: a class onboarded with such a group or
domain keeps it and stays editable (a value read back unchanged is not judged), and the dialog cannot set one. The
`backend` knob is bounded like a name, not checked against the registry: on a class whose module file names no
`role:` — a hand-made file; the tool always writes one — a backend the registry does not ship, or one that declares
no role, is stored, and the next onboard that names no backend answers 409, saying to give the class a `role:` (one
that names a backend the registry runs plans with that backend's role). The `role` knob is bounded like a name too,
not checked against `ansible/roles/`: a role that does not exist is stored, adopted by the next onboard and written
as every new host's `device_role`, and its plays fail where they include it — the closed-set check is the linker's
(S36), the step that already reads both trees. (The two names kontroll itself made up before S26,
`backend_vendor_config` and `backend_napalm`, the knob refuses.) A backend the classifier chooses is written
into a new class's module file under the registry entry's own `name`, which nothing holds to the name rule: an entry
named outside it yields a module file whose next onboard answers 409 (shipped data; a test holds every shipped
entry's name). And `plain_scalar` is a rule about how YAML reads a word, not about how a person reads it: characters
outside ASCII that format or space text — the bidirectional controls, a zero-width space, a no-break space — are
plain words to it (the byte-order mark and the line and paragraph separators are not). No request field that is held
to it admits a non-ASCII character today: every request charset is an ASCII class. Two sets are a descriptor
author's to choose — a `text` knob's pattern and an `enum` knob's allow-list — and in Python `\w`, `\d`, `\S`, `.`,
a negated class and a class under `(?i)` all reach beyond ASCII, so the rule is owed with the first knob that uses
one. Free text that is serialized rather than formatted — a capture exception's subject and notes — is not held to
the rule at all, and the serializer writes such a character as it is.
**Proves:** `tests/unit/test_request_boundary.py` (every validator's accept/reject table including `..`, separators,
NUL, a newline — also as the last character —, an IPv6 zone id, a 65th char and a 254-char hostname; `confined()`
against `../`, absolute and climbing paths; the planner refusing before the probe is reached, also for a class key
YAML reads as another type; `deep_probe` refusing before `ansible-doc` starts; the telemetry, logging, backup,
identity, host-vars and suggest builders refusing before the filesystem is asked; a refusal's message never carrying
the value; the enumerators skipping what is not a key; every onboard field that becomes a name refusing a keyword
while a host name may be one; no boundary pattern accepting a trailing newline even with `match()`),
`tests/unit/test_yamlscalar.py` (`plain_scalar`, `quoted` and `word` against every position a writer uses —
exhaustively for short strings over the indicator alphabet, the line-break characters and the Unicode separators
included), `tests/unit/test_validate.py` and `tests/unit/test_blockwrite.py` (the `text` and `hostname` knobs, the
zone id on the address knobs, the identity names bounded like the join, the writers failing closed on a keyword, a
line break and a value that is not a scalar), `tests/unit/test_settings.py` and `tests/unit/test_identity.py` (a
writer's refusal is `bad_value`, not an exception; a value read back unchanged is not judged, one that is not a
string still is; a module file that is not a mapping), `tests/unit/test_observe.py` and
`tests/unit/test_fetch_dashboards.py` (the dashboards shape, including the name that rewrote a class's role with a
newline and the one that rewrote the item with none, one name with two board ids, a board with two names, an
existing file that is not the board, a damaged module file, and the fetch never writing over a file that is not the
board it fetches), `tests/unit/test_gen_observability.py`, `tests/unit/test_gen_backup.py` and
`tests/unit/test_actuation_service.py` (a group, host or key that is not a plain word is quoted and reads back as
itself), `tests/unit/test_create_unit_service.py` and `tests/unit/test_actuation_registry.py` (the pin's charset and
the digest; a unit's collection and name against the hostile table, at the validator and at the create route, which
reads the validator's own pattern; a derived key too long for one path component answered before the class is
looked up; the page's example unit passing the validator; the rendered lockfile and sidecar reading back as what
was resolved; a trust statement that cannot be rendered as itself, also at the promote gate; the keyring),
`tests/unit/test_fleet.py` and `tests/unit/test_discovery.py` (the service readers of a fleet file with a line
that is not a key), `tests/unit/test_fleet_line.py` (the same line at every deploy-time reader: the classifier's
places and its silence about the entry; no generator asking the filesystem about it, each saying which entry it
skipped, the in-process regenerations returning; a file with no list rendered and pruned by no renderer, and a
capability change over it refused before it writes; the collection generator refusing in each of its seats — a
line, a file with no list, a file the parser gives up on, a file that cannot be opened — and passing a key with
no directory; the plays' check writing nothing, and refusing to ride with another mode; the promote gate refusing
the line and every proposal behind it, saying when the fault is on `main`, and the removal promoting on a bare
canonical; the check task of the bootstrap, of the deploy and of the canonical's commit-and-mirror, by position,
each stopping its play, each running under `--check` and leaving no bytecode behind) and
`tests/unit/test_deploy_stack_check_mode.py` (no generator that writes runs under `--check`; `--fleet-check` is
a read-only mode there only with the script that implements it),
`tests/unit/test_onboard_ssh_key.py` (the lookup expression, the inline literal at the planner's call site, the
stem), the API pins in `tests/integration/test_api_onboard.py` (git never touched; the code in the body),
`test_api.py` (the open routes, with no token and no audit line; a fleet line that is not a key) and
`test_api_capability.py`, the GUI pins in `tests/integration/test_gui_api.py` (the capability, identity, host-vars,
suggest and configurable routes), `tests/unit/test_api_audit.py` (one request value is one cell of one audit row,
read back through the real file), `tests/unit/test_paths_overlay.py` (the overlay probe: a climbing path is absent,
a symlinked overlay is found), `tests/unit/test_repository_data.py` (each adopted name against the hostile table;
the refusal naming the class, the file and the rule and never the value; a module file that is not a mapping; every
shipped module and backend passing the adoption rule; a module file with a malformed block degrading, not crashing;
a credential field's `maps_to`, and an inline field's host var; the role a class gets — the registry's own, never
one made from a name — by whose fault, a matched backend that declares none passed over, and that the role a new
class's plan or the scaffold writes is one the next onboard adopts), `tests/unit/test_backend_registry.py` (every
role a backend declares has a check and a backup; each backend that declares none, and one the registry does not
ship, refused before anything is probed; the auto path, the search record's suggestion and `galaxy classify`'s
`best` choosing only a backend that runs; a reused class keeping its own role whatever shipped backend is named,
an unshipped one still refused, and one naming the role the planner used to make up refused); that no word the
identity knob accepts is refused by the
adoption rule; the reserved names at every seam
that chooses a group or names a host, the loopback addresses among the host's; the targets an apply asks about
before its first write, and the apply not shadowing a class the code root ships; the scaffold refusing what the
planner refuses, writing under the write root and not shadowing a shipped class; the recipe name; `confined()`'s
type and the clean-up that must keep skipping what it cannot confine; the command line's one clean line, and the
group and domain it names), `tests/unit/test_authspec.py` (an installed module's option whose name is not an
identifier is not offered, and a module with a required or secret one is not used; a declared entry that is not
well-formed is skipped), with the route pins in the integration files (409 `repository_data` on the preview and on
the apply of both onboard routes; on the capability, identity, unit-create and unit-stage routes when a write target
does not confine, and on the GUI's read routes: nothing written, the audit action and its kind; the identity route
on a module file that is not a mapping; a reserved host name as a 422 on both apps), and the C19 paint gate for the
Fleet index.

---
## Accepted risks / known limitations

| Item | Risk | Accepted? | Rationale |
|---|---|---|---|
| Single operator, no RBAC beyond Semaphore | Anyone with control-VM access controls the fleet | Yes | Single-user homelab; Semaphore adds per-user RBAC for the UI layer in Phase 3 |
| **R-KEY-1** — age key loss | Encrypted secrets become **permanently** unrecoverable | Yes (**uncompensated** for an instance with one recipient) | **Corrected 2026-07-28; narrowed 2026-10-23 (a break-glass choice at `--fresh`).** An instance created by `kontroll-init --fresh` with `--break-glass-recipient` or `--mint-break-glass` has a second recipient on every domain from its first secret. An instance created with `--no-break-glass`, or before `--fresh` asked, has **ONE** recipient per domain: losing its control key destroys every device credential, API token and service password in it. Secrets are low-volume and re-enterable *in principle*, but re-entering them means recovering each one from its device, and for a token-only credential (an API token) the plaintext exists nowhere else and must be reissued on the device. **Fix for such an instance:** [docs/break-glass.md](docs/break-glass.md) — add the recipient, re-wrap, verify. The risk stays open for every instance whose break-glass key has not been proven to open every domain (kontroll counts recipients; it cannot know whether the private half was kept), and an off-box **copy of the private key** never closes it — a copy shares the single point of failure. See [C1](#c1--secrets-encrypted-at-rest-sops--age). |
| API rate-limit counters in-process | Counters reset on restart + are per-process (wrong if the API is ever run multi-worker / multi-replica) | Yes | One uvicorn container today (mgmt-only, single-operator); the store is a swappable seam → move to a shared backend (Redis) the moment the API scales horizontally |
| API rate-limit `X-Forwarded-For` trust | A spoofed XFF could shift an inquiry IP bucket | Yes | mgmt-VLAN-only; any fronting proxy is operator-controlled. When SSO/proxy lands (cluster 2c), trust XFF only from the known proxy hop |
| Private git remote holds encrypted secrets | Push exposes ciphertext | Yes | SOPS ciphertext is safe to host; the age key is never pushed |
| Control VM on a hypervisor it also manages | A bad play against that node could affect the controller | Partial | Snapshot + git + offline key; avoid self-targeting the controller's node in risky plays |
| Current edge is FortiGate/Cisco (pre-cutover) | Edge creds + API differ from the post-cutover target | Yes | Functional groups isolate the churn to one inventory block |
| `host_key_checking = False` (TOFU) | MITM on a device's *first* connect isn't detected | Yes | Known fleet on the trusted mgmt VLAN; the alternative is a manual per-host `ssh-keyscan` that breaks hands-off setup. Key *changes* after first contact still surface via the recorded known_hosts. |
| Prometheus `--web.enable-lifecycle` (item F) | Anyone reaching `:9090` can `POST /-/reload` (benign) **or** `/-/quit` (stop Prometheus — a DoS) | Yes | Enables the no-docker-socket `reload-observability` enact task (HTTP reload, no host root). `:9090` is **mgmt-bound** (C3, never WAN) — the same boundary that already gates the whole control plane; `/-/quit` only stops a restart-`unless-stopped` container (no data loss). Net: a clean Tier-1 enact for a mgmt-only DoS surface. |
| Device config captures are not encrypted at rest | A backup capability with `destination: offsite` would move secret-bearing captures (running-config hashes/PSKs) outside the local `0700`/uid-1001 store with no encryption-at-rest story | Yes (deferred) | The backup-capability ships `destination: local` only — captures stay `0700`, git **never pushed** (C8). The dialog now exposes a `destination` knob (#123) that **offers** `offsite` for discoverability but **refuses it at propose** (`backup.build_plan` → `bad_param`, fail-closed with the C8 reason), so no offsite copy can be promoted until an encryption-at-rest design closes this. `retention` is likewise declared/carried (`kontroll_backup_retention`) and its prune **enforcement** is now LIVE (#125 — a fail-closed, default-safe history rewrite of the local captures repo only; see C8). C1's `kontroll_backup_deploy` backs up the **encrypted repo**, not device captures. |
| Captures are READABLE over the GUI backup viewer (C14) | An authenticated mgmt-VLAN operator — or a GUI-process compromise (which already holds the age key → fleet root, C10) — can read device running-configs (hashes/PSKs/communities) over HTTP, **redacted-by-default but best-effort** (a novel vendor token shape can leak into the view) | Yes (compensated; **gated ship** G1–G5 incl. human sign-off) | **`:ro` mount** (no write-back, compose-pinned on the `:ro` suffix), **read-only git only** (no actuation/rewrite — AST + argv pins), **rev/file validated before any `git show`** (traversal/injection closed), **redacted-by-default + redact-then-diff** (defense-in-depth, not the boundary), behind the GUI's **fail-closed auth + TLS + mgmt-VLAN bind** (C3), **every read audited NAMES-only**. The secret-densest vendor (FortiGate, history-excluded) is **listed but never rendered** (its on-disk blob stays off the wire). Captures stay `0700`/uid-1001 at rest, history **local-only, never pushed** (C8 unchanged). Marginal exposure dominated by the existing onboard-gui-with-key residual (C10). Raw download **deferred** (C2). See [C14](#c14--capture-read-over-the-gui-is-a-read-only-romounted-redacted-audited-surface). |
| The network API/GUI becomes a canonical-writer (C10) | A compromised/leaked-token service could push to the source of truth every clone + deploy trusts | Yes (compensated) | **Propose-then-promote**: the service only pushes a `proposed/<run_id>` staging ref — a trusted promote step fast-forwards `main`, so a leaked token can only stage a proposal: it cannot promote one, and nothing it stages reaches `main` — the only tree an enact reads — before a trusted promoter has landed it (under `separated`, a human who can reject it; [docs/trust-mode.md](docs/trust-mode.md) says what `solo` changes). **No age key** is mounted (no standing decrypt). `receive.denyNonFastForwards`/`denyDeletes` + a push-only clone + rate-limit + mgmt-VLAN-only (C3) bound it. **Corrected 2026-07-05:** the "only parks rejectable proposals" property held for `push:false` but was **breached by `push:true`** — the content clone's `origin` **is** the canonical, so `commit_and_push`'s optional `git push origin HEAD` advanced `main`. Closed by the `and not staging` guard (`gitio.py`) + the canonical's `update` hook (an allow-list since 2026-10-13: as uid 1001 a push may create `refs/heads/proposed/<run id>` and nothing else; it does not see a write that is not a push). That a staging service never advances `main` is structurally true again (`docs/reviews/2026-07-05-c10-origin-push-breach/` — a design record kept in the private repository). See [C10](#c10--privileged-mutation-enablement-the-network-service-writes-the-canonical---live-verified-gated-by-api_privileged) + [docs/privileged-mutation-enablement.md](docs/privileged-mutation-enablement.md). |
| Arming (`api_privileged`) gives every network container `:rw` on the canonical (C10, item C) | A **container RCE** (api/onboard-gui/semaphore) can `update-ref`/rewrite/delete `main` **directly** — `update-ref` is not a push, so deny* + the FF-only gate (which bound the promote *playbook*) do **not** constrain it; a breach can non-FF/delete main, not just stage | Yes (compensated) | Gated on `api_privileged` — the **read-only deploy mounts `:ro` everywhere** (default), so this exists only when armed. The leaked-**token** path stays strictly propose-only (`from_env`+`_push_target` fail-closed coupling). Bounded: the canonical is a **reconstructible cache** (operator workstation + GitHub are truth — re-pushable, divergence visible), and the **dominant** residual (fleet root via the scoped age key onboard-gui/semaphore already hold) strictly dominates canonical-write; mgmt-VLAN + single-operator. The alternative (Semaphore SSH-to-host + sudo) is strictly worse (host root). **Narrowed 2026-07-05, 2026-10-13:** the canonical's `update` hook refuses every push from the service's uid but the creation of a proposal, so the tool's own code cannot push `main`, a tag or a second version of a proposal. The hook runs as the pusher: it is a check on what the service's code does, not a bound on a container RCE. The residual is a uid-1001 RCE using `git update-ref`/`packed-refs` rewrite **directly** (not a push — the hook only fires on receive-pack) — unchanged, still bounded by the reconstructible-cache + offsite-mirror-fsck follow-on. **Narrowed 2026-10-13 (2):** what such a writer leaves in the canonical's own files no longer reaches a git that runs as ROOT through a promote — every git the promote, the listing, the discard and the export start there as root is the service uid (C10, rule 5); the promoter still reads the commit it is asked to land, as its caller. It does not narrow what the row is about: a writer that moves `main` decides what the next deploy, and every Semaphore job, runs from it. |
| Secret values + a minted private key cross the mgmt-side hop (C11) | A submitted secret / a show-once private key travels in the request/response body | Yes | TLS (GUI) or the operator's reverse proxy (`byo_proxy`, API) protects it; **mgmt-VLAN-only, never WAN** (C3), behind C9 fail-closed auth. Symmetric to the already-accepted secret-entry exposure. The private key is **show-once** and never persisted; `.sops.yaml` edits are additive + parse-verified. See [C11](#c11--guided-secret--key-onboarding-is-no-leak-by-construction). |
| ~~Vector runs root with a `:ro` docker socket~~ → **HARDENED: non-root + socket-proxy** (C12) | A Vector RCE could `POST /containers/create` = host root (the raw socket is root-equivalent even `:ro`) | **No — closed (GATE A passed)** | Vector now runs **non-root** (uid 10002) and holds **no raw socket** — `docker_logs` targets a `docker-socket-proxy` (`POST=0`, GET/HEAD-only); journald via the discovered `systemd-journal` GID; audit dirs via `o+r` (never `group_add:1001` — it would leak the TLS keys). A Vector RCE is now an unprivileged log-reader: through the proxy it can list containers, inspect them (each one's inspect JSON, its Config with `Env` among it and its Mounts: the stated residual) and read their logs, and since 2026-10-21 nothing else under `/containers` (before, the stock grant also passed `archive`, `export` and a websocket `attach`; the proxy does not start without that limit; `test_socket_proxy_start_script.py`). **GATE A dogfood (2026-06-18): PASSED** — non-root Vector shipped 1822 journald events + container logs through the proxy; `POST` → 403. **Promotion unblocked.** See [C12](#c12--the-logging-pipeline-is-a-secret-surface-collector-privilege-label-hygiene-mgmt-only). |
| Loki stores plaintext log bodies + has no auth (C12) | A log line may carry a secret an upstream device failed to redact; Loki itself is unauthenticated | Yes | The store is `0750`/uid-10001 at rest (C8 boundary), mgmt-VLAN-only (C3), queryable only through Grafana (`access: proxy` — the browser never touches Loki); the stack's own logs are `no_log`-clean (C2). **The hardening is an opt-in basic-auth reverse-proxy gateway** — a gated Caddy `loki_auth` service, OFF by default; Loki stays single-tenant `auth_enabled:false` behind it; Grafana + the Vector sink carry the cred from a `logging_loki_auth` SOPS domain — **not** Loki's own `auth_enabled` (that is X-Scope-OrgID *tenancy, not authentication*, and breaks the `fake`-tenant store). `loki_auth: off` is a strict no-op. **Un-defer trigger:** Loki `:3100` reachable beyond the single-operator mgmt VLAN, OR the C9 GUI/API SSO residual closes — whichever first (this is the FIRST of the three to un-defer). Blueprint: `docs/reviews/2026-06-18-logging-ingress-hardening/99-synthesis.md`. |
| The syslog ingress (`:5514`) is unauthenticated + mgmt-bound (C12) | A host on the mgmt VLAN could send forged/spoofed log lines into Loki | Yes | Plain RFC5424 syslog has no auth by design; the port is **mgmt-VLAN-only** (`${KONTROLL_MGMT_IP}`, never WAN, fail-closed `:?`), TCP, and **only listens when a class declares `syslog_push`** (else nothing binds inside the container). Forged lines are labeled `source=capability` + the device key (never trusted as audit). **The hardening is mTLS over a shared control-issued CA** — Vector's `syslog` source has **no PSK mode**; mTLS client certs are the only auth it offers (`verify_certificate: true` ⇒ OpenSSL `PEER|FAIL_IF_NO_PEER_CERT`, so the handshake aborts for any sender without a CA-issued client cert and forged lines are rejected *before* Loki). **Opt-in per class** (a class declares a TLS-syslog method; plaintext stays for classes that can't speak it) with a **fail-closed-at-generate** contract (an incomplete TLS declaration stops the build, never silently downgrades to plaintext). **Un-defer trigger:** the syslog ingress becomes reachable beyond the single-operator mgmt VLAN (a cross-VLAN/WAN sender). Blueprint (+ the per-vendor framing dogfood, e.g. FortiGate RFC6587 octet vs Vector field-parse): `docs/reviews/2026-06-18-logging-ingress-hardening/99-synthesis.md`. |
| The journald-upload receiver (`:19532`) is unauthenticated + mgmt-bound + runs root (C12) | A host on the mgmt VLAN could upload forged journal entries; `systemd-journal-remote` runs as root | Yes | `systemd-journal-upload` has no auth by design; `roles/logging_journald_receiver` binds the receiver to the **mgmt IP only** via `--listen-http={{ logging_collector_host }}:19532` (a specific IP, never `0.0.0.0` — pinned by `tests/unit/test_journald_receiver.py`), and it is **only provisioned when a class declares `journald_remote`** (deploy-stack gates on the rendered drop-in). Uploads are labeled `source=capability` (not trusted as audit). **The root half is CLOSED:** the receiver now runs as the **non-root `systemd-journal-remote`** user (`Group=systemd-journal`, `NoNewPrivileges`, `ProtectSystem=strict`; `test_journald_receiver.py`). The remaining **unauthenticated** half is **BLOCKED ON UPSTREAM, not merely deferred:** `systemd-journal-remote` with `--listen-https` + `TrustedCertificateFile` **neither requires nor verifies client certs** (systemd#4092, OPEN in 2026) — TLS would negotiate but authenticate nothing; additionally, Debian-family dropped the `--trust=` client-CA option moving journal-remote off GnuTLS. There is **no upstream-supported authenticated journald-upload on Debian.** **Un-defer trigger:** systemd ships OpenSSL client-cert verification for `journal-remote`. **Interim:** for a Linux host that needs authenticated upload, prefer the TLS-syslog path (a local rsyslog/syslog-ng client with a CA-issued cert), which *is* authenticatable. Blueprint: `docs/reviews/2026-06-18-logging-ingress-hardening/99-synthesis.md`. |
| `proxmox_api` defaults to `verify_certificate: false` for PVE's self-signed cert (C12) | Vector accepts the PVE host's self-signed cert, so a mgmt-VLAN MITM could impersonate the PVE API to Vector | Yes (default) / **hardenable** | The posture is **no longer a bespoke hardcode** — it's DERIVED from the device-class vendor fact (`modules/proxmox` `vendor_defaults.tls_posture: self_signed`, captured-from-public + pinned) via `scripts/kontroll/endpoints.py`, the SAME fact the pve-exporter derives from (no drift). The poll is **read-only** (a forged response could at worst inject false log lines labeled `source=capability`, never actuate), **mgmt-VLAN-only**, and uses an **audit-scoped read-only token**. **Hardening is fully wired end-to-end, not a TODO:** set `device_trust.proxmox.tls_ca_file` (a repo-relative HOST path to your PUBLIC CA PEM) in `instance.yml` → deploy-stack assembles ONE CA bundle and BOTH consumers verify against it — the Vector pull (`source.tls.ca_file` → the mounted bundle) AND the pve-exporter (`REQUESTS_CA_BUNDLE` + a `:ro` mount; `PVE_VERIFY_SSL` is bool-only). No per-class hub edit; the instance trust DECISION is distinct from the vendor fact. Pinned by `tests/unit/test_endpoints.py` + `test_gen_logging.py` + `test_gen_exporters.py`. |
| `file_tail_ssh` mounts a read SSH private key into Vector (C12) — **host-key TOFU now CLOSED** | A Vector RCE could read the SSH private key and `journalctl -f` the targeted hosts (the read is forced-command-locked) | Yes (key) / **No (TOFU closed)** | The key is a **dedicated forced-command** key (`journalctl -f -o json` only — no shell, no arbitrary file read; the tightest possible grant), `0600` owned by the **non-root Vector uid** (10002, not root), mounted **RO**, and **only present when a class declares `file_tail_ssh`** (else an empty placeholder). The forced read can at worst expose the target's own journal to a compromised collector — never actuate. **The host-key TOFU is closed:** the exec ssh now uses `StrictHostKeyChecking=yes` against an **operator-pinned, read-only** `known_hosts` (assembled from `device_trust.<key>.ssh_known_hosts_file`, like the device-CA bundle) — an unpinned/changed key makes ssh REFUSE (fail-closed, the stream blanks; deploy warns), never a silent MITM-accept. Pinned by `tests/unit/test_file_tail_ssh.py` (`test_exec_ssh_pins_host_key_no_tofu`, `…_bind_mounts_the_pinned_known_hosts_ro`, `…_assembles_known_hosts_always_written`). |
| The device admin SSH **private** key is materialized to an ephemeral `0600` file at fleet-play time (C12 — the unified password-OR-key onboard seam, G6) | A key-auth host's private key (SOPS-encrypted at rest) is decrypted + written to a `0600` file in the runner's per-run dir so `ansible_ssh_private_key_file` can use it; a runner compromise *during* a fleet run could read it | Yes | The key never touches git / the inventory drop-in / a `--check --diff` / the Semaphore job log — SOPS-encrypted at rest (C1), pasted via the GUI + written via sops' **STDIN** (never argv), then materialized `no_log` to a `0600`, run-uid-owned, per-run dir **OUTSIDE the repo clone** (`_render-ssh-keys.yml`) and removed by `_cleanup-ssh-keys.yml` (a hard-aborted run leaves one `0600` key in a per-run dir until the next run/reboot — accepted). Pinned by `tests/unit/test_onboard_ssh_key.py` (PEM-via-stdin-not-argv; render-task-`no_log`; the password-only backward-compat golden; the loud-fail-on-undecryptable). **Scope decision (B):** the scoped Semaphore key is **NOT** widened to `compute`, so a `docker_host` SSH key is **control-node-only** — a key-auth host whose domain the runner can't decrypt **fails LOUDLY** (the `_render-ssh-keys.yml` assert), never a silent no-key. **`file_tail_ssh`** keeps its dedicated forced-command read key (key-only by necessity — `BatchMode`, no `sshpass`, and a password would break the no-shell forced command): a deliberate least-privilege exception to the seam, which scopes the device ADMIN/connection credential only. |
| **R-NB-1** — the fleet supply chain is verified by `==` pin + `sha256` only when a signature is absent (C15-a) | On public Galaxy **100% of the fleet's collections serve no signature** (verified 2026-06-25); the novice default installs on the `==`+`sha256` floor with **no publisher-identity proof** — a malicious upload at the *pinned name+version* whose tarball matches its own manifest would pass | **Yes (the conscious never-brick trade)** | The alternative — fail-closed on absence — **bricks 100% of novice deploys** (the operator-stated invariant forbids it); public Galaxy serves nothing more, permanently. The floor is real + unconditional: the exact `==` pin closes silent version-bump (`resolve_pins`, `gen-requirements.py`), the recorded `sha256` closes tarball-swap of the pinned artifact (wrapper L2b, fail-closed on mismatch — next rung). The residual is **narrowed** by the pin, the human-added `modules/` gate (fleet), the **Tier-cap** for downloaded units against high-blast targets (`gen-actuation.py`), and the loud `unsigned-source` surfacing so it's never silent. **Un-defer trigger:** a signing-enabled source (Automation Hub / self-signed mirror) becomes the default → ratchet `signature_policy: required` against it (to be built — the policy changes no install today, C15-a). mgmt-VLAN, single-operator (C3). See [C15-a](#c15a--supply-chain-install-is-never-brick-adaptive-pinning--checksum-signature-where-served). |
| **R-INST-1** — the compose-native installer holds a `:rw` docker socket = host root for the run's duration (C16) | The ephemeral installer can do anything the host daemon can (create privileged containers, read any host file) while `scripts/kontroll-installer.sh <verb>` runs | Yes (compensated) | Its blast radius **equals** today's host-ansible install (which already runs as the operator with sudo + writes the same crown jewels), with a **narrower** dependency surface — a pinned image vs the unpinned host toolchain that caused the fix(install) bug class. **Ephemeral** (`run --rm`, no `restart:`, no `ports:`) so the grant is not a standing surface; **no secret in any image layer / named volume** (host binds only); mgmt-VLAN, single-operator (C3). The Phase-1 image is a **local build** (trusted == today's host toolchain); **C1 now publishes a digest-pinned installer image** (Docker verifies the `@sha256:` on pull = image-world L2b, R-IMG-1), pulled by `scripts/kontroll.sh` from a launch kit instead of built on the node. See [C16](#c16--the-compose-native-installer-is-an-ephemeral-host-bind-host-root-trusted-surface). |
| ~~**R-IMG-1** — pulling the published images while the packages were PRIVATE added a `ghcr.io` read credential to the control node (C0/C1)~~ → **RETIRED 2026-10-11: the three packages are public, every pull is anonymous** | Under `use_published_images=true` the control node did `docker login ghcr.io` with a `read:packages` PAT to pull `kontroll-{control,vector,installer}` while those packages were private — a host credential the local-build path didn't need. A new package starts private whatever the repository's visibility (the first publish from this public repository, 2026-10-09, produced private packages); its owner makes it public once, by hand (no API), and a public container package cannot be made private again | **No — closed (public since 2026-10-11; the visibility change is one-way, so no package setting can bring the credential back)** | **No registry credential on the control node under either path:** default `use_published_images=false` builds locally; `use_published_images=true` pulls the public packages anonymously (verified 2026-10-11 from a credential-less registry token: `v0.1.2` resolves by tag and by digest to the lock). CI pushes with the built-in `GITHUB_TOKEN` (no long-lived push secret). Every image is pinned by `@sha256:` in `docker/images.lock.yml` and **Docker verifies the digest on every pull** — the image-world L2b (the bytes CI recorded are the bytes that run; a registry-side swap fails the pull). C1 publishes the **installer** image too; the `scripts/kontroll.sh` bundle launcher pulls it the same digest-verified way, and the launch kit's `images.env` carries only public `@sha256:` digests — never a secret. A box that still carries a `docker login ghcr.io` credential from the private period can `docker logout ghcr.io`; nothing reads it. Mgmt-VLAN, single-operator (C3). The air-gap alternative (`docker save` in the bundle) reaches no registry. Cosign *verification* is the deferred opt-in rung (mirrors the never-brick keyring). See [docker/README.md](docker/README.md). |
| **R-DISC-1** — passive discovery reaches an onboarded device with its read cred + surfaces an untrusted lease / neighbour table (C18) | The discovery sweep reads a firewall/router/switch's own view with the class's read cred — a DHCP-lease API over an HTTP **GET** (the `network` domain) OR an ARP/neighbour table over an SNMPv3 read-only **`snmpbulkwalk`** (the shared `snmp_observability` v3 user); a compromised sweep host could read those, and a hostile/spoofed device could return a crafted lease/ARP table into the inbox | Yes (compensated) | **Read-only + passive by construction** — a byte-capped GET or a byte-bounded, killed-on-overflow SNMP WALK, no write/scan/CIDR/SET primitive (grep-gated), each reaching ONLY an onboarded device's own address; the cred is reused **as-scoped** from an existing SOPS domain, `${ENV}`-only at the boundary (operator-exported for SNMP, not deploy-provisioned), audited by NAME. The lease/ARP row is **untrusted DATA**: byte+row capped, every field re-validated on the pure read side (`_validate` — a bad IP dropped, a bad/null MAC → None, incomplete-ARP noise dropped), rendered `textContent` (Rung 1b). **No new authority** — the sweep is a top-level actor OUTSIDE the pinned layer; `read_inbox` is `assert_read_only`-green, so discovery gains **no** promote/actuate path, and a human still onboards+promotes every surfaced host (C10 unchanged; no auto-onboard). Control-VM/on-demand, never an install-gating path (BRICK-1); mgmt-VLAN, single-operator (C3 — which also covers the net-snmp `-A`/`-X` argv-passphrase residual). The runtime artifact is git-ignored; committed fixtures are documentation-only (P-1, OID-aware grep-gated). See [C18](#c18--passive-discovery-is-a-read-only-cred-reusing-untrusted-response-surface). |
| **R-DISC-2** — passive discovery reads an OpenWrt AP's dnsmasq lease file over an SSH forced-command read (C18) | The discovery sweep authorizes a dedicated SSH key on each `wireless_ap` host and reads `/tmp/dhcp.leases` over it — a new SSH grant on the AP; a compromised sweep host could use it, and a hostile/spoofed AP could return a crafted lease file into the inbox | Yes (compensated) | **Read-only + passive by construction** — the key is FORCED to `cat /tmp/dhcp.leases` (no shell, no arbitrary file read, no port/agent/X11 forwarding, no tty — the tightest grant, the `logging_file_tail_ssh` least-privilege precedent), a **DEDICATED** key (never the full-shell `ansible_ed25519` root key on `wireless_ap`), and the client SSH sends **no** command word (the AP's forced command is the only thing that runs). Host-key **PINNED** — `StrictHostKeyChecking=yes` against an operator-pinned read-only `known_hosts` (NO TOFU; a changed key makes ssh REFUSE, the source blanks fail-closed, never a silent MITM-accept), `BatchMode=yes`/`IdentitiesOnly=yes` (no password/agent fallback). A write/tunnel/remote-command SSH is **un-expressible**: the `discovery-passive-only` positive gate forbids the write/tunnel/indirection/spawn constructs, and `test_ssh_read_is_forced_command_read_only` (an argv-AST twin) proves no client command word follows `user@host` (what a grep can't see). The lease row is **untrusted DATA** — byte-capped at the transport (streamed, killed-on-overflow), row-count-capped at parse, every field re-validated on the pure read side (`_validate` — bad IP dropped, bad hostname/MAC → None), rendered `textContent` (Rung 1b). The private key is the **`network`** SOPS domain, `${ENV}`-PATH-only at the boundary (operator decrypts to an ephemeral `0600` file + exports the path at invocation — the `gen-validate-live`/C12 onboard-key precedent, never deploy-provisioned, never a job log). **No new authority** — the sweep is a top-level actor OUTSIDE the pinned layer; `read_inbox` stays `assert_read_only`-green (no promote/actuate path), a human still onboards+promotes (C10). Control-VM/on-demand, never an install-gating path (BRICK-1); mgmt-VLAN, single-operator (C3). Committed fixtures are documentation-only (P-1; the dnsmasq client-id column is `*`). See [C18](#c18--passive-discovery-is-a-read-only-cred-reusing-untrusted-response-surface) + the `openwrt_discovery_key` role. |

---

## Update this document when…

- A new credential field or secret domain is added to `instance/secrets/`.
- A new managed device class (and its creds path) is added.
- A firewall rule for the control VM is added or widened.
- The trust boundary changes (e.g. exposing any UI beyond the mgmt VLAN).
- A new privileged surface (UI or API) is added, or its auth/audit/exposure changes.
- A new log source, collector, or log-label schema is added (the logging pipeline is a secret surface — C12).
- A dependency with security relevance (SOPS, age, a collection that handles
  creds) is added or changed.
- The control plane's binding/exposure (NPM, ports) changes.
