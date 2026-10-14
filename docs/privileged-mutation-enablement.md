# Privileged-mutation enablement — the decision record (C9 → C10)

> **Status: DECIDED + building.** This is the design-of-record for turning on live actuation from the deployed
> API/GUI (the "C9 enablement"). It supersedes the open questions in the dated research reports
> [`docs/reviews/2026-06-13-roadmap/01-design-1a-privmut-enablement.md`](reviews/2026-06-13-roadmap/01-design-1a-privmut-enablement.md)
> (the mechanics) and [`…/01-design-1b-privmut-security.md`](reviews/2026-06-13-roadmap/01-design-1b-privmut-security.md)
> (the threat model). **Posture chosen: propose-then-promote, no key (1b's recommendation #2).** Read this first
> if you're picking the work up cold.

## 0. The one-paragraph version

The deployed API/GUI is a **network service** — a program that runs all the time, listens on the mgmt IP (`<mgmt-ip>`),
and acts on any request bearing a valid token, with no human at the keyboard. Without `api_privileged` it is
deployed **read-only** (no token ⇒ every privileged route 503s; the content clone root-owned and the canonical
mounted `:ro`; no age key in the API) — it physically cannot mutate. Turning
it on means handing an always-on, reachable **deputy** the power to change the source of truth. We do that under
one uniform rule: **the network service may only PROPOSE a change (it pushes a staging ref); a trusted step
(you, or a Semaphore approval task) PROMOTES it to `main`.** And **no decrypting age key** is mounted into the
service. A leaked token can then only *stage proposals*, and the API holds no key to read a secret with. Under
`separated` you reject or promote each proposal, so the token alone never rewrites the lab
([trust-mode.md](trust-mode.md) says what `solo` changes).

## 1. The three layers (only the first is gated)

The word "actuation" hides three independent things. Propose-then-promote touches **only the first.**

| Layer | What it is | Gated by this decision? |
|---|---|---|
| **1. Provenance** | *Who/what may change the canonical repo* `/srv/kontroll.git` `main`. | **YES — this is the whole decision.** And only for the **network service**; *your* direct pushes from your working tree stay direct (you're the trusted human). |
| **2. Enactment** | *Does a committed config change actually do anything?* No — committing a `metrics:`/`backup:` block actuates nothing; the API runs no play. The enact commands (deploy-stack / fetch-dashboards / `gen-backup`+`configure-semaphore` / prometheus reload) are a separate hand-off **you** run. | **NO — pre-existing, independent gate** (the PROPOSE/ENACT boundary the capability seam already has). Unchanged. |
| **3. Scheduled autonomy** | *Do scheduled jobs need per-run approval?* No — once a backup schedule is in the canonical + registered in Semaphore, it runs **autonomously on its cron, forever, unattended.** | **NO — propose-then-promote does NOT preclude scheduled actions.** The gate is only on how the schedule's *config* entered the repo, never on the schedule *running*. |

So: "does any action ever require human approval?" → **only one: the network service changing the source of
truth.** Your commits, the enact hand-off, and every scheduled job are untouched.

## 2. Why this posture (the eyes-open reasoning)

- **The token is all-or-nothing.** Setting `KONTROLL_API_TOKEN` lights up **every** privileged route on the
  service — including `onboard-apply`, which can write a new module/role + enable it fleet-wide (a HIGH-blast
  write a deploy would run). You cannot enable only the low-blast capability promote; they share one
  fail-closed gate (`api/auth.py`). So the *whole* write surface needs to be made safe — and one uniform rule
  ("the deputy only stages") does that for the low-blast capability writes and the high-blast onboard writes
  alike, with no per-route reasoning.
- **No key, because encrypt-without-decrypt isn't possible with age.** SOPS re-encrypts a file in place on
  `--set`, which requires *decrypt* of the file's data key — so mounting a key to *write* a cred inherently
  grants *decrypt* of that whole domain to the always-on service (1b §4.3). The capability writes carry **no
  creds** (telemetry/backup are data-only), so they need no key at all; onboard's cred-encryption moves to the
  promote step (a trusted runner holding the scoped key does it out-of-band). Net: **zero standing decrypt
  capability in a network service** — 1b's single biggest risk, eliminated.
- **It preserves the human gate you deliberately drew.** The read-only deferral was protecting exactly this:
  no network service writes the truth unreviewed. Propose-then-promote keeps that gate; it just *relocates* it
  from "the route is disabled" to "the route stages, you promote."

The discarded alternative (1a's direct-push + a mounted scoped key) is 1b's ranking #3 — "acceptable floor,
mgmt-VLAN + single-operator only," strictly weaker. We are not taking it.

## 3. The mechanics (what gets built)

Four pieces. What switches them on is **`api_privileged`**. Its default is the `api_privileged:` line of
`ansible/playbooks/deploy-stack.yml` — `false` in the public tree; another edition may ship another — and a
deploy without it is the read-only one: no token, root-owned content clones, the canonical mounted `:ro`. Two
things below do not wait for it: the canonical's own hardening in piece 3 (`receive.deny*` and the `update`
hook, set by `local-canonical.yml` on every install) and the operator's CLI promote in piece 4.

1. **Token** — `kontroll_api_token` lives in the `dashboards` SOPS domain (service-secrets; control + break-glass
   recipients only — **not** the Semaphore key, so a Semaphore breach can't read it). `deploy-stack` renders it
   into `docker/.env` (gated) and injects `KONTROLL_API_TOKEN: ${KONTROLL_API_TOKEN:-}` (soft-default) into the
   API/GUI env. Setting it is what *arms* the surface (the `auth.py` 503 gate inverts automatically — no code).
2. **A dedicated push-only clone** — the service gets its own writable clone (`…/api/repo-rw`, uid-1001), with a
   `local` remote → `/srv/kontroll.git`. It is **never** a writable mount of the canonical itself.
3. **Staging-ref push** — when the service runs (`KONTROLL_STAGE_PUSHES=1`, set only in the container env),
   `gitio.commit_and_push` pushes `HEAD:refs/heads/proposed/<run_id>` instead of `main`. The operator's CLI (no
   such env) still pushes `main` directly. The bare repo gets `receive.denyNonFastForwards` +
   `receive.denyDeletes` (recommended **unconditionally** — protects your own pushes too; kills history rewrite).
   **After a staged push the service resets its clone to the canonical `main`** (`git fetch local && git reset
   --hard local/main`) so its read-back ("current") always reflects the source of truth and each proposal is
   **independent** off `main` — never a stack of un-promoted (or rejected) edits that a later promote would carry
   along (F1, dogfood 2026-06-20; `tests/unit/test_staging_isolation.py`). The CLI's `main` push is never reset.
   **The optional `origin` push is the operator CLI's alone.** Under staging, `commit_and_push` does **not** push
   `origin` at all (`and not staging`): the content clone's `origin` is the canonical itself (a `file://` clone),
   so `git push origin HEAD` there would fast-forward `main` — a network-service direct-main write, the exact thing
   this posture forbids (closed 2026-07-05, dogfood VM 144; `tests/unit/test_staging_isolation.py`
   `test_staging_push_origin_never_advances_canonical_main`). The canonical's `update` hook
   (`ansible/playbooks/files/canonical-update-hook.sh`) refuses such a push at the canonical as belt-and-braces:
   it is an allow-list — as uid 1001 a push may create `refs/heads/proposed/<run id>` and nothing else.
4. **The promote action** — `kontroll-promote.py <run_id>@<commit_id>` (CLI verb, operator-run = trusted)
   fast-forwards `main ← proposed/<run_id>` **only if it is a fast-forward** and **only if the proposal is still
   the commit named** (else it refuses), then deletes the proposal — see "The promote lands one commit" below.
   **The in-GUI follow-on is built (item C, 2026-06-15):** the **`promote-proposal`** Semaphore template
   (`config/semaphore/templates/promote-proposal.yml` → `ansible/playbooks/promote.yml`) wraps the same verb,
   driven by a `run_id` survey var that takes `<run id>@<commit id>`. It runs as **gid 1001** against the canonical mounted `:rw` (gated on
   `api_privileged` via `KONTROLL_CANONICAL_MODE`; `core.sharedRepository=group` makes new objects group-writable
   so no host root is needed). The promoter is **Semaphore** (admin-authed, mgmt-VLAN) — **not** the API token,
   so the proposer can never self-promote.

   **The GUI surfaces pending proposals read-only (#129):** a header **"Pending"** panel (`GET /api/pending` →
   `kontroll.service.pending.list_pending`) lists every `proposed/<run_id>` ref + a one-click copy of
   `<run id>@<commit id>`, so the operator can feed the `promote-proposal` survey without shelling into the box
   (there, `kontroll-promote.py --list` prints the same form). It is a
   pure read of the canonical — it **never promotes** (no Promote control, no `promote_ref`/push import), so the
   two-key split is intact: the GUI proposes and *shows*, Semaphore promotes.

No age key is mounted (deliberately). Onboard-apply's `sops_set` is therefore deferred to the promote step (a
trusted runner with the scoped key encrypts the staged cred *names* into the domain on promotion) — see §5.

## 4. Build plan (commit-sized; CI-green per commit) — ✅ ALL BUILT (live-enable is the operator's, §5)

1. ✅ **`docs` (this file) + `SECURITY.md` C10 + CHANGELOG** — the decision record (`b264be2`).
2. ✅ **The staging-ref push + the `promote` CLI verb + tests** (`a403c1f`) — `gitio.commit_and_push` gains a
   `run_id`; when `KONTROLL_STAGE_PUSHES=1` it pushes `proposed/<run_id>` not `main`; the API onboard/capability/
   capture-exception routes + the GUI capability route thread it; `gitio.promote_ref` + `scripts/kontroll-promote.py`
   are the FF-only promote. Pinned by `tests/unit/test_privmut_staging.py` (stages-not-main, FF-only, refuse-non-FF).
3. ✅ **The flag-gated deploy enablement** — `docker/services/api.yaml` (token + stage env soft-defaults, the
   content-mount writability controlled by clone OWNERSHIP, **no age key**), `docker/services/onboard-gui.yaml`
   (stage env + `user: "1001:1001"` + the `/srv/kontroll.git` mount, mirroring `api.yaml` — keeps its
   host-provisioned age key for device-cred encryption), `ansible/playbooks/deploy-stack.yml` (`api_privileged`
   flag — now tier-wide: gated token render + gated chown-to-uid-1001 + `local` remote on **both** the API clone
   AND the onboard-gui clone, so the GUI capability dialog stages too), `ansible/playbooks/local-canonical.yml`
   (uid-1001 write grant via shared-group+setgid + the **unconditional** `receive.denyNonFastForwards`/`denyDeletes`
   hardening), `docker/semaphore-runner/Dockerfile` (baked git identity). Everything new is gated
   `when: api_privileged` so the **read-only deploy is provably untouched** (and each clone stays
   root-owned ⇒ uid-1001 can't write it). CI lints; the operator applies + live-verifies on first gated deploy.
4. ✅ **The covering checks** — the privileged routes 503-without-token / 401-200-with are already pinned
   (`tests/integration/test_api_auth.py`); the staged push targets `proposed/*` not `main`
   (`test_privmut_staging.py`); the deployed compose carries **no** age-key mount (`tests/unit/test_privmut_no_key.py`).

## 5. Going live — ✅ DONE (verified live 2026-06-15) + the from-scratch recipe

**Live-verified on the VM 2026-06-15:** the auth gate inverts (no-token 401 / valid 200), the API mounts **no
age key**, a privileged write **stages** `proposed/<run_id>` (never `main`), `kontroll-promote.py`
fast-forwards `main`, and `receive.denyDeletes`/`denyNonFastForwards` are enforced. The token was rotated after
the test. **onboard-gui parity (2026-06-15):** the gated deploy now provisions the onboard-gui clone identically
(uid:gid 1001:1001 + `local` remote + the `/srv/kontroll.git` mount); a `run_id` push from that clone staged
`proposed/<run_id>` with `main` untouched, and `update-ref -d` rejected the proposal cleanly — so the **GUI**
capability-promote path is live too, not just the API. The from-scratch recipe (the lessons folded in):

1. **Token:** on the VM, `sops instance/secrets/dashboards.sops.yml` → add
   `kontroll_api_token: "$(openssl rand -hex 32)"` (a secret — born + encrypted on the VM, never committed/transcripted).
2. **Canonical hardening + write grant** (`local-canonical.yml` does the top dir + deny-non-FF; existing objects
   need the one-time recursive grant): re-run `local-canonical.yml` — its detect-then-fix tasks chown
   `<operator>:1001` and grant `g+w` + dir-setgid **outside `hooks/`**, which stays root-owned `0755` (a gid-1001
   writer must never be able to plant a hook; SECURITY.md C10). Never a blanket `chmod -R g+w` of the canonical.
3. **Gated deploy:** `ansible-playbook playbooks/deploy-stack.yml -e '{"stack_services":["api"],"api_privileged":true}'`.
   It renders the token + `KONTROLL_STAGE_PUSHES=1`, clones + chowns the API tree to uid-1001, adds its `local`
   remote, and rebuilds the runner image (the baked git identity). **Live lessons now baked into the artifacts:**
   the API runs **`user: "1001:1001"`** (gid 1001 to write the group-1001 canonical) and **mounts
   `/srv/kontroll.git`** (so the `file://` remote resolves in-container) — both in `api.yaml`; the deploy avoids
   `become_user` (no `acl` dependency) and sets `safe.directory` so the clone task is idempotent.
4. **Promote — the `promote-proposal` Semaphore task (item C):** arm with `api_privileged` incl. `semaphore` in
   `stack_services` (so Semaphore is recreated mounting the canonical `:rw` via `KONTROLL_CANONICAL_MODE=rw`),
   run `python3 scripts/configure-semaphore.py` to register the template, then run **promote-proposal** in the
   Semaphore UI and enter `<run id>@<commit id>` (the Pending panel's copy control). It fast-forwards `main` as
   gid-1001 (no host root — the `core.sharedRepository=group` + B group-write grant make the proposal
   refs/objects group-writable). CLI fallback: `sudo python3 scripts/kontroll-promote.py --list --repo
   /srv/kontroll.git`, then `sudo python3 scripts/kontroll-promote.py <run_id>@<commit_id> --repo
   /srv/kontroll.git`. With `sudo` the command is root only until it starts git: every git it runs in the
   canonical is the service uid (1001:1001), so it writes the same files, as the same owner, as the Semaphore job
   does — and says so on stderr. Without `sudo`, an account that is neither root nor the service is refused.
5. **Verify:** `curl -sk -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" https://<mgmt>:8444/audit/log`
   (`http://` under `frontend.tls_mode: byo_proxy` or `acme`, where the API serves plain HTTP to your proxy)
   → 200 (no token → 401); a capture-exception/capability promote returns `staged:true target_ref:proposed/<run_id>`
   with `main` unchanged; `sudo docker inspect kontroll-api` shows `/repo` rw + `/srv/kontroll.git` + **no** age key.

### The promote lands one commit (decided 2026-10-13)

The promote used to be a sequence of steps that each looked the proposal's NAME up for itself: a preview, a
gate, a fast-forward check, the move of `main`, the delete. Between any two of them the name could come to mean
another commit, so what was previewed, what was judged and what landed could be three commits. Decided:

- **One read, then ids.** `kontroll-promote.py` reads `main` and the proposal once (by full ref name, in the
  seam of `kontroll.canon`), and everything after is asked of the two commit ids. The move is one ref
  transaction (`gitio.promote_commit`) that verifies the proposal is still that commit and moves `main` only
  from the commit it was read as. Judged is promoted, or nothing moves.
- **A human names the commit.** The approval is `<run id>@<commit id>` — the commit the operator was shown. The
  `promote-proposal` job requires it; the box's shell accepts a bare run id (it promotes what the read returns,
  and prints it first). A proposal that is no longer the named commit is refused. Whether the job requires the
  pin is the maintainer's call; making it optional is the flag and the assert's pattern in `promote.yml`, and the
  rows of `tests/unit/test_canon.py` that hold them.
- **The promoter's printed preview is the record.** Before it moves anything the promote prints the run id, the
  commit, and the commits being landed, from the ids it will use. That — in the Semaphore job log, or on the
  terminal — is what was approved; the Pending panel is how one finds it.
- **The gate fails closed** (the entry before this one): a tree it cannot read, or a gate that cannot run, is a
  refusal. `--skip-conflict-check` therefore means "the check could not run and the node must move" or "I have
  read this proposal myself" — it is no longer the way past a gate that had quietly skipped itself.
- **Nobody is told to run git in the canonical by hand.** The play's raw `git` tasks and the messages that
  suggested `git -C /srv/kontroll.git …` are gone; `kontroll-promote.py --list` and the Pending panel answer the
  same questions through the tool's own reader.
- **What is in the canonical is named, not assumed** (the same day). Every deploy runs
  `python3 -B scripts/kontroll-canonical.py --audit` over the canonical — before it writes anything into it — and prints, by
  kind and key, what it finds there that the play and the tool do not write: a config key, a hook, a ref of
  another kind ([troubleshooting.md](troubleshooting.md) §8). Names, not values; and it reports — it does not
  stop the deploy.
- **A promote by root does not run git as root** (the same day). A promote by `sudo` runs every git as the service
  uid, because the files git reads there are ones a network service writes when the deploy is armed
  (SECURITY.md C10). Who else may run it is the maintainer's call. Built: root and the service uid only, where
  the repository is one the service can write; a member of the canonical's group who is neither is told to use
  `sudo` — run as themselves, git would open that store with their account. Letting group members through is
  the one refusal in `kontroll-promote.py` that says so, printed as a note instead, and the row of
  `tests/unit/test_kontroll_promote.py` that holds it.

## 6. What's deferred (named, not silently dropped)

- **Armed containers can write the canonical directly (a container-RCE residual, not a token one).** When armed,
  all three network containers (api, onboard-gui, semaphore) mount `/srv/kontroll.git` `:rw` as gid 1001 — so a
  *code-execution* compromise of any of them can `git update-ref`/rewrite `packed-refs` **directly**, bypassing
  `receive.deny*` + the FF-only gate (those bound the promote *playbook*, not a compromised container). The
  leaked-**token** path stays strictly propose-only (`from_env`+`_push_target` fail-closed coupling; the API never
  calls `promote_ref`). Mounts are gated `:${KONTROLL_CANONICAL_MODE:-ro}` so the **read-only deploy is `:ro`
  everywhere**; this exists only when armed. Accepted, bounded by the reconstructible-cache + age-key-dominance +
  mgmt-VLAN (SECURITY.md C10 residual + accepted-risk row). A future hardening (offsite mirror push / `git fsck`
  integrity check on the canonical) would make a silent rewrite detectable. **Update 2026-07-05:** a uid-1001
  `update` hook on the canonical (`ansible/playbooks/files/canonical-update-hook.sh`, installed by
  `local-canonical.yml`) now refuses a receive-pack **push** to `main`, so the container-RCE-does-a-*push* case is
  caught. **Update 2026-10-13:** the hook is an allow-list — as uid 1001 a push may create a proposal and nothing
  else (no tag, no `refs/replace/*`, no second push to a proposal that exists); from any other uid, `main` and a
  proposal. The remaining residual is specifically a direct `update-ref`/`packed-refs` rewrite (not a push — the hook
  fires only on receive-pack, the same reason it never blocks a legit `promote_ref` update-ref), which the
  offsite-mirror-fsck check would make detectable. (H2 — converting the deploy mirror to `update-ref` so the hook
  can be unconditional rather than uid-scoped — is a tracked follow-on; not in this change.)
- **Onboard-apply's cred-encryption over HTTP.** With no key in the service, an HTTP onboard that carries creds
  stages the host block + cred *names* only; the encrypt happens at promote by a trusted runner. The
  out-of-band encrypt step is **not built yet** — until it is, HTTP onboard-*with-creds* is incomplete (the
  capability promotes, which carry no creds, are fully functional). This is the honest boundary of the no-key
  posture.
- **onboard-gui is the with-key exception (not no-key).** Unlike the API, `docker/services/onboard-gui.yaml`
  mounts the scoped age key (`SOPS_AGE_KEY_FILE`) because the GUI alpha does device-cred encryption inline
  (`gitio.sops_set` needs decrypt-to-add — the same "age can't encrypt-without-decrypt", 1b §4.3). So wiring
  onboard-gui's capability-staging live (this change) adds **bounded, rejectable canonical-write** to a surface
  that **already holds a standing decrypt key**. The no-key property is therefore an **API** property, pinned
  only for `api.yaml` (`tests/unit/test_privmut_no_key.py`). The GUI's standing-decrypt is the dominant residual
  and is **pre-existing** (the device-onboard alpha always held it), bounded by onboard-gui's own fail-closed
  auth + mgmt-VLAN-only. Closing it needs the same deferred out-of-band encrypt step; until then the API is the
  no-key surface and onboard-gui is the with-key one. Tracked in SECURITY.md C10 residual.
- **Rate-limiting as a hard prerequisite.** Already built (`api/ratelimit.py`); 1b treats it as a prerequisite
  for any write-capable surface — it is in place.
- **SSO / token rotation** (C9 residual) — static mgmt-VLAN-only token for v1; SSO is a later cluster.

## See also
- [SECURITY.md](../SECURITY.md) C9 (the fail-closed surface) + **C10** (this enablement's control)
- [docs/api-architecture.md](api-architecture.md) §8 (the actuation boundary) / §9 (rollout)
- [docs/observability/secondary-capability-dialog.md](observability/secondary-capability-dialog.md) — the
  capability seam whose promote this enables (its writes are data-only / no-cred — the easy half)
- the dated research: [1a mechanics](reviews/2026-06-13-roadmap/01-design-1a-privmut-enablement.md) ·
  [1b threat model](reviews/2026-06-13-roadmap/01-design-1b-privmut-security.md)
