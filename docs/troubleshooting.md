# Troubleshooting runbook — where each signal lives + how to read it

The operability companion to [logging-architecture.md](logging-architecture.md). When something looks wrong,
this tells you **which label each kontroll surface lands under**, the **queries** to run, and — crucially — how
to keep debugging **when the observability stack itself is down** (the degrade path). All of this is also a pane
in the Grafana **kontroll — Logs** dashboard (`dashboards/grafana/dashboards/logs.json`); the queries here and
the dashboard panels are kept in sync on purpose.

> Conventions: every log line carries the canonical, non-secret label set `{source, host, service, level,
> run_id, device}` (C12). `source` splits **internal** (the stack's own logs) from **capability** (device/service
> logs ingested via the logging capability). `run_id` correlates one operation across surfaces and is set on
> **internal** streams only (device logs are not part of a kontroll run).

## 1. Where each signal lives (the label map)

| Surface | `source` | `service` | Notes |
|---|---|---|---|
| Container stdout/stderr (every stack service) | `internal` | the **container name** (`vector`, `loki`, `semaphore`, `api`, …) | from the Docker source |
| Host journald (systemd) | `internal` | the **unit** (`docker.service`, `sshd.service`, …), else `host` | persistent journal |
| API audit (who/when/what on the API) | `internal` | `api-audit` | kept **90d**; never the credential |
| GUI audit (onboard-GUI actions) | `internal` | `gui-audit` | kept **90d** |
| Ansible run-log (playbook runs) | `internal` | `ansible` | carries the `run_id` of the run |
| Script run-logs (CLI helpers) | `internal` | the **script name**, else `script` | from `~/.local/state/kontroll/runs` |
| A device/service's own logs (logging capability) | `capability` | the **method** (`syslog_push`, …) | + `device=<module key>` (e.g. `cisco_ios`) |

## 2. Common LogQL queries (Grafana → Explore → Loki, or `logcli`)

```logql
# Everything from one operation, greppable by its correlation id (internal only):
{source="internal", run_id="20260616T143000Z"}

# A specific surface (audit trail kept 90d):
{source="internal", service=~"api-audit|gui-audit"}

# Anything wrong, across the whole stack:
{source=~".+", level=~"(?i)(warn|warning|err|error|crit|critical|alert|emerg|fatal|panic)"}

# One device class's ingested logs (the logging capability):
{source="capability", device="cisco_ios"}

# Free-text body search (a |~ filter — a query, never a label, so no cardinality cost):
{source="internal"} |~ "(?i)backup.*fail|captured nothing"

# Volume of a chatty emitter (sizing-lever input; pairs with the dashboard "Top services" panel):
topk(10, sum by (service) (count_over_time({source="internal"} [1h])))

# What the "Container restart / crash storm" alert counts, by container (its rule sums the same over 5m). Run it
# when the alert fires, and over the last 7 days before changing the rule: Grafana logs the rule's uid at every
# evaluation and each deploy prints `TASK [Restart …]` banners, so a looser filter counts lines that are no crash.
sum by (container_name) (count_over_time({source="internal"} | json container_name, message | __error__=""
  | container_name != "" | line_format "{{.message}}" |~ "(?i)restart|panic|exited"
  !~ `kontroll-log-container-restart|^(TASK|RUNNING HANDLER) \[` [5m]))
```

## 3. The degrade path — debugging when Loki/Vector is down

Observability must survive an observability outage. Loki or Vector being down loses log **shipping**, never the
source logs — every surface persists locally and is readable without the stack. Per the access-chain header in
`docker/services/vector.yaml` ("Fallback required: no … degrade to `docker logs`/journalctl/the flat audit files"):

| You want… | When the stack is up | When Loki/Vector is down |
|---|---|---|
| A container's logs | the `internal` stream by container name | `docker logs <name>` (bounded by daemon.json `max-size 10m ×5`) |
| Host/systemd logs | the journald `internal` stream | `journalctl -u <unit>` on the control VM |
| API audit | `{service="api-audit"}` | the flat TSV at `/var/lib/kontroll/api/audit/` |
| GUI audit | `{service="gui-audit"}` | the flat TSV at `/var/lib/kontroll/onboard-gui/audit/` (M11 — off the `/repo` code/propose clone) |
| Ansible run-log | `{service="ansible"}` | the flat log at `/var/lib/kontroll/ansible-log/` |
| Script run-logs | `{service=<script>}` | `~/.local/state/kontroll/runs/` |

Bring the stack back: `docker compose ... up -d loki vector` (or re-run `deploy-stack.yml` with `loki`/`vector`
in `stack_services`). Vector resumes from its on-disk checkpoint (the `vector-data` volume), so a restart
re-ships from where it stopped rather than re-reading everything.

## 4. Quick checks

- **No logs in Grafana at all** → confirm the Loki datasource provisioned (`uid: loki`); confirm Vector is
  Up and shipping (`docker logs vector | grep -i "Listening\|Healthcheck"`); confirm Loki `/ready`
  (`curl -s ${KONTROLL_MGMT_IP}:3100/ready`).
- **Vector logs `Error in communication with Docker daemon` about every ten minutes** → the socket proxy is
  cutting idle log streams: its log-stream route is missing. `docker logs docker-socket-proxy | grep kontroll:`
  says why (Vector excludes the proxy, so its WARNING never reaches Loki). Now and then the error is real:
  `timeout client 10m` still cuts a stream Vector stopped reading, e.g. while Loki was down.
- **A device's syslog never arrives** (`{source="capability"}` empty) → confirm `docker/services/vector.yaml`
  publishes the mgmt-bound `:5514`, the device is configured to ship there (the `logging_syslog_push` wiring
  role), and the device can reach the collector on the mgmt VLAN.
- **Disk filling** → metrics are bounded by `PROM_RETENTION_SIZE`/`_TIME`; logs by `LOKI_RETENTION_PERIOD`
  (+ per-stream tiers); see the storage paradigm
  ([docs/reviews/2026-06-16-storage-logging/99-synthesis.md](reviews/2026-06-16-storage-logging/99-synthesis.md)).

## 5. An answer that is not about your request — 409 `repository_data`

An onboard, or an identity / capability / unit preview or apply, answers **409** with `code: repository_data`
when what stops it is the repository's own data or layout. Nothing in the request is wrong: the remedy is the
file or the layout. The sentence says where to look:

| The sentence names | The cause | The remedy |
|---|---|---|
| a device class and `modules/<key>/module.yml`, with a rule | the class's module file holds a group, secrets domain, backend or role that is not a name (lower-case letters, digits, `_`, `-`; starting with a letter or digit; at most sixty-four characters; not a word YAML reads as another type), a group Ansible reserves (`all`, `ungrouped`, `localhost`), or — at an onboard or an identity edit — is not a mapping at all | correct that file; a group rename re-homes the class (move its hosts in `instance/inventory/onboarded-<key>.yml`, rename its `group_vars` file if it has one, regenerate, dry-run the deploy) |
| a device class and a backend's credential field | a credential field is declared — in the class's `auth:` block or the backend's registry entry — with a name, or a host var, that is not an identifier, or as `inline` with no host var | correct the declaration |
| a device class whose backend needs a `role:` | the class's backend — adopted from its module file, or chosen from the backend registry — names no role, and `backend_<name>` is not a name (too long, or outside the charset); or the registry entry's own `role` is not a name | give the backend a `role:` in its registry entry (`ansible/backends/<name>/backend.yml`), or correct the one it holds; for a class that already has a module file, a `role:` there works too |
| a write target that does not resolve inside the repository | `instance/` or `modules/` is a symlink out of the write root, or `KONTROLL_WRITE_ROOT` points at another tree | make the directory a real directory of the write root (a bind mount is one), or fix the variable |
| a block key of this class's `module.yml` (`metrics`, `logs`, `backup`, `dashboards`) written with a value on the key's own line, or held twice; or "the text it would write does not read back as the change asked for" | the capability dialogs edit a block — the key alone on its line (a comment after it is fine), its entries on the lines below. A flow list on the key's line is not one; a file that holds the key twice is read by YAML as its LAST block only (a capability change made before the writer could see a commented key line left such a file); and a list the writer cannot follow line by line — a comment line at the left margin inside the block, a quoted method or key, one method declared twice — would have been written with an entry twice, so the plan is refused instead | rewrite the key as a block; or merge the two blocks into one by hand — `yamllint` names the line (`key-duplicates`); or indent the comment with the entries, unquote the name, and keep one entry per method. The file is `modules/<key>/module.yml` in your instance repository; if the class is a shipped one your repository does not hold, the copy read is the image's — put a corrected copy at that path in the repository, which is read in its place. Commit, bring it to `main`, then try again |

The audit log has `repository-refuse what=<kind> route=<route>` for it — not `request-refuse`, which is a field
the caller got wrong (422). `POST /onboard` has another 409, without a `code`: a drop-in that exists with
different content; that one is retried with `overwrite`.

## 6. A fleet line that is not a device-class key

`instance/fleet.yml`'s `enabled_modules` is a list of device-class keys. A line that is not one is skipped by
everything that renders and refused by what installs, and the bootstrap, a deploy and a promote each stop on it.
Every message names the ENTRY by its place among the list's entries, counted from one — comment lines, blank
lines and commented-out entries are not counted — and never repeats the line.

| What you read | Where | What it means | What to do |
|---|---|---|---|
| the task `Check that every enabled_modules entry is a device-class key (…)` fails with `gen-requirements: instance/fleet.yml: enabled_modules entry N is not a device-class key …` | the bootstrap; a deploy started part-way through its stack play (`--start-at-task`) | the play stops before anything walks the list; no generated file was rewritten | correct entry N in the node's checkout: a key is lowercase letters, digits, `_` and `-`, starting with a letter or digit. An unquoted `no`, `on`, `off`, `yes`, `~`, a number or a date is another type to YAML — the message then says "YAML does not read it as text": quote it, or rename the class. Then run the play again |
| `local-canonical: REFUSING to commit — gen-requirements: instance/fleet.yml: …` | a deploy (its canonical play, before the stack play), also the dry-run | the deploy's own commit would have carried the line, and the landing after it would have been refused by the gate; nothing was committed or landed on the canonical | the same; then deploy again |
| `REFUSED: promoting proposed/<run_id> — the proposal's tree does not resolve …`, and under it the generator's sentence, `gen-requirements: refusing to resolve the collection set — instance/fleet.yml: enabled_modules entry N …` | the `promote-proposal` job, or `kontroll-promote.py` | the tree the promote would make `main` holds such a line. If the message adds that `main` as it stands is refused as well, the line is on `main` already (a deploy of an older build mirrored it unjudged) — and then EVERY proposal is refused, whatever it changes | if the line is in the node's checkout (a hand edit there is how it usually reached `main`): correct it there and deploy — the deploy's commit-and-landing takes the correction to `main` (the landing judges the corrected tree, not `main` as it stands). Otherwise promote a proposal that removes or corrects it: the Settings view's `disable` control stages the removal of a line that is plain, unquoted text; for any other line (`no`, `~`, a number, a quoted name, one with a slash) make the proposal by hand — in the node's checkout (or a clone of the canonical with a `local` remote) correct `instance/fleet.yml`, commit, `python3 scripts/kontroll-land.py --propose <twelve lowercase hex digits>` (it judges the tree first and creates `refs/heads/proposed/<id>`; never `git push` — the canonical's hook refuses `main` from everyone and the tool pushes nothing by hand), and promote that id; then bring the node's checkout forward (`git fetch local && git rebase local/main`) before the next deploy. A proposal staged before the correction still carries the old line and is refused with the same sentence: discard it and propose again |
| `<generator>: ignoring: instance/fleet.yml: enabled_modules entry N …` | a generator's stderr: the `kontroll-api` or `onboard-gui` container's log (the `internal` stream) after a capability change; a generator run by hand | that generator skipped the line and rendered the rest | as above |
| `<generator>: not rendering: instance/fleet.yml holds no list of classes …`; from the API or the GUI, a 409 `repository_data` that says `instance/fleet.yml holds no list of classes …` | a generator run by hand; a capability change (telemetry, logging, backup) | the file is not a mapping, or `enabled_modules` is not a list: nothing was written and nothing was pruned, and the capability change was refused before it wrote the module file | restore the file's shape: one `enabled_modules:` key over a list; then make the capability change again |
| `gen-requirements: instance/fleet.yml is not YAML this tool can read` — with `(line L, column C)` when the parser gives a place — or `gen-requirements: instance/fleet.yml cannot be opened (…)` | a deploy, a promote (at the bootstrap its own `Load fleet profile` task fails first, with Ansible's message) | the file does not parse, or is not there to read | correct the file — at that place, when one is given |

A line that IS a class key and has no directory under `modules/` is none of these: the bootstrap stops on it
(its own lookup) and nothing else says a word (SECURITY.md C22, the limits).

## 7. A promote that is refused for the proposal's tree, or because the gate could not run

Before it fast-forwards `main`, a promote reads the proposal's commit — the commit itself, object by object, each
re-hashed against its id — and hands its files to this checkout's collection generator. It refuses (exit 2,
`main` untouched, the proposal still staged) when the tree is not one it takes, and — with a different sentence —
when it could not judge at all. The gate's own sentences name the rule, never a path or a value of the proposal.
When it is the generator that refuses, its last lines are shown: they can name a module file and, for a
`signature_policy` that is not a word it takes, repeat the value — escaped.

| What you read | What it means | What to do |
|---|---|---|
| `REFUSED: promoting proposed/<run_id> — the proposal's tree does not resolve …` and under it `the tree cannot be read as it was committed: the tree holds an entry that is not a regular file or a directory (a symbolic link, a submodule, or a mode git does not write)` | the proposal commits a symbolic link or a submodule entry (or a file mode written by hand). Nothing this tool writes does; a link in `main` is a file a later step would open through | discard the proposal (Pending panel) and stage the change again without the link. If the link is on `main` already (the message then says `main` is refused as well), remove it in the node's checkout and deploy |
| … `the tree holds an entry whose name no checkout could take` | a file or directory name with a path separator or a control byte in it, `.`, `..`, any spelling of `.git` that git's own checkout refuses (`git~1`, `.git` with trailing dots or spaces or a `:stream`), a name longer than a file system takes, or the same name twice in one directory | as above — such a tree was written by hand |
| … `an object is not what its name says`, `an object the commit names is missing`, `an object is not the kind its place in the tree asks for` | the canonical's object store does not hold what the commit names: a corrupted or replaced object file | do not override. The canonical is a reconstructible cache: rebuild it from the node's checkout (a deploy re-creates `main`) and stage the proposal again. Do not run git by hand in the canonical or in a copy of it — a copy carries the canonical's own `config` |
| … `git ended on an object the commit names: the object store holds one git cannot read` | git died while it read one object of the proposal — a damaged object file, not a missing one | as the row above: do not override; rebuild the canonical and stage again |
| … `the tree holds a path that is not UTF-8` / `two paths that this filesystem takes for one` / `git's answer for an object is not in the form that was asked for` / `the commit does not name a tree` / `a tree object is not in the form git writes` | a tree or a commit written by hand | discard the proposal; find out who wrote it |
| … `the tree is larger than the gate reads` / `holds more entries than the gate reads` | the proposal's tree is far beyond anything this tool stages (the caps are `MAX_TREE_BYTES` and `MAX_TREE_ENTRIES` in `scripts/kontroll/canon.py`; a node whose own tree has outgrown them promotes with the override until they are raised) | look at what the proposal changes (Pending panel) before anything else |
| … `the tree has no modules/ directory and main has one: promoting it would remove the device-class registry` | the proposal deletes `modules/` | discard it |
| … `the tree has no fleet file … and main has one` | the proposal deletes `instance/` (or the legacy `config/fleet.yml`) | discard it |
| `REFUSED: the ref proposed/<run_id> is not a proposal: it is a symbolic ref, or it names no commit` (exit 1) | the ref under that run id was not staged by the service (the Pending panel lists it under "Not proposals") | nothing can be promoted from it; find out who wrote it |
| `REFUSED: refs/heads/main is missing, or is not a branch that names a commit …` (exit 1) | `refs/heads/main` in the canonical is absent, a symbolic ref, or names a tag — nothing this tool writes | nothing is promoted onto it: rebuild the canonical from the node's checkout |
| `REFUSED: run this with sudo. The repository is one the network service can write, and you are neither root nor that service …` (exit 1) | `kontroll-promote.py` — or its `--list` — was run on the canonical by an account that is neither root nor uid 1001; nothing was read | run it with `sudo`: it then runs git as the service uid — not as you, and not as root. If this shows for a repository of your own, something in it is owned by uid 1001, writable for gid 1001 or for everybody, or is a symbolic link (on a workstation, gid 1001 can be your own account's group): take that one thing out — `sudo` does not help there, the service cannot read a clone under your home |
| … `the canonical could not be read (could not drop to the service uid; nothing was read or changed)` (exit 1) | the command is root but may not change uid and gid: a container started without `CAP_SETUID` / `CAP_SETGID`, or a user namespace that denies `setgroups`. Nothing ran as root instead | run it where root can change identity (the host's shell with `sudo`; the installer container as shipped) or as the service uid itself — the Semaphore `promote-proposal` job does |
| `kontroll-promote: git runs as the service uid (1001) in this repository, not as root` (stderr; not a refusal) | you ran the promote as root on a repository the service can write; it is the record of as whom git ran | nothing |
| `REFUSED: say which commit: <run id>@<commit id> …` (exit 1) | the `promote-proposal` job (or a promote run with `--require-pin`) was given a bare run id | copy the pinned form from the Pending panel's row ("copy to promote"), or print it with `kontroll-promote.py --list --repo /srv/kontroll.git` |
| `REFUSED: main is not the commit this promote was to land on: it is <commit> now …` (exit 1) | the promote was run with `--onto <commit id>` — by a caller that judged the proposal against that `main` — and `main` has moved since | nothing was promoted; the proposal is judged again against `main` as it is (run the caller again) |
| `REFUSED: proposed/<run_id> is not the commit you were shown: it is <commit> now …` (exit 1) | the proposal was staged again under the same id after you read it | open the Pending panel again and read the row as it is now; approve that, or discard it |
| `! refuse: the proposal is no longer the commit that was judged …` (exit 1) | the proposal was staged again, or removed, in the moment between the promote's read and its move; nothing was promoted | open the Pending panel again and read the row as it is now; approve that — with its own `<run id>@<commit id>` — or discard it |
| `! refuse: main moved while this proposal was being promoted …` (exit 1) | another promote landed in that moment; nothing was promoted | run the promote again — it reads, previews and judges afresh against the new `main` |
| `! refuse: git refused the transaction although both refs are as they were read: a lock file is held in the canonical …` (exit 1) | another promote was running at that instant — or a git that was stopped (a cancelled job, a killed process) left its lock files behind: `HEAD.lock`, `refs/heads/main.lock`, `refs/heads/proposed/<run_id>.lock` in the canonical. Or this user cannot write the canonical | run the promote again. If it is refused the same way with nothing else running, the lock files are left-overs: with no promote, deploy or stage in flight, remove those `.lock` files (and only those) and promote again. `main` has not moved |
| `! refuse: git could not run the promote on the canonical, or did not finish — …` (exit 1) | git is not installed, the canonical is one the tool does not read (a `commondir` file, a part that is a link), or git did not answer in time | the rest of the sentence says whether `main` is this commit. If git was stopped it may have left lock files — the row above |
| `~ note: promoted main, but could not delete proposed/<run_id> (stale ref left behind)` (exit 0) | `main` IS the commit; the proposal's ref could not be removed (a lock, a permission on a ref the service created) | nothing is wrong with `main`. The Pending panel lists the left-over ref as not promotable: discard it there |
| `REFUSED: proposed/<run_id> is main's commit already: there is nothing to promote …` (exit 1) | that left-over ref, or a proposal staged at `main` | discard it (the Pending panel) |
| `REFUSED: the commits this promote would land could not be listed (…)` (exit 1) | git did not answer, or its listing was not in the form asked for; nothing was promoted | run it again; if it repeats, the reason in brackets says which |
| `REFUSED: proposed/<run_id> is not a fast-forward of main …` (exit 1) | `main` has moved past the commit the proposal was staged on (the Pending panel marks it stale) | stage the change again against current `main`; discard the stale proposal |
| `REFUSED: promoting proposed/<run_id> — the gate could not run, so the proposal's tree was not judged:` and under it the reason — `this checkout has no scripts/gen-requirements.py …`, `the collection generator could not be started` / `did not finish in N seconds`, `git could not be run on the canonical`, `the canonical holds a commondir file …` | nothing was judged, so nothing was promoted. Not a verdict on the proposal | correct what the reason names and promote again. `--skip-conflict-check` promotes WITHOUT the gate — for a node that must move while the gate cannot run; read the proposal's change list first |
| `… the gate could not run …` and under it `git did not answer for an object within the time the reader gives it` | git went silent on one object for the reader's whole time limit (`TREE_READ_SECONDS` in `scripts/kontroll/canon.py`). A slow disk once; the same object every time is a damaged object file | run the promote again. If it repeats: as the object-store row above — do NOT override |
| `… the gate could not run …` and under it `a file of the tree could not be written where it was to be read` | the promote could not write its temporary copy: the temporary directory is full or not writable | free space in the temporary directory (`TMPDIR`) and promote again |
| `… the gate could not run …` and under it `the canonical's <part> is a symbolic link …` / `git could not read the canonical` / `the canonical lists one ref name more than once` | the canonical itself is not as this tool makes it | nothing is promoted from it: rebuild the canonical from the node's checkout |
| `REFUSED: that is not a run id …, or what follows the @ is not a full commit id` (exit 1) | the argument is not `<run id>` or `<run id>@<commit id>` as the service and git write them | copy it from the Pending panel |

## 8. The audit of the canonical named something

Every deploy runs `python3 -B scripts/kontroll-canonical.py --audit /srv/kontroll.git` (the task "Audit the
canonical" of `local-canonical.yml`) and prints what it names; you can run it yourself, from the checkout, as any
account that can read the canonical (a part an account cannot read is reported `unread`, so root and you can get
different reports). It changes nothing in the canonical and it does not stop the deploy. Each line is a KIND and
a KEY — the name of a config key, of a ref, of a file — never a value. Of owners and modes it reports the ones
that decide whether the `update` hook runs and who may replace it, and no others
(`sudo find /srv/kontroll.git -maxdepth 1 -printf '%u:%g %m %p\n'` shows the top level); it does not look into
the object store. A canonical that holds only what the play and the tool write prints nothing.

**A deploy audits BEFORE it writes.** The play puts a canonical right on every run — it writes `HEAD`, sets its
own config keys, installs `hooks/update`, makes `hooks/` root's — and the audit stands in front of all of that:
the report is of the canonical as it has been since the last deploy, and the dry run (`--check`) prints the same
one. So what the play puts right is named in the run that puts it right, and not again: **a line of that kind
that comes back on the next deploy is something that changes the canonical between deploys.** An update that
itself brings a newer hook, or sets a key the play did not set before, names that hook or that key once, in its
first deploy; that one is nobody's doing. A node's first deploy has no canonical yet and audits nothing.

What the tool itself puts into the canonical is: `refs/heads/main`, `refs/heads/proposed/<run id>`, the objects
they name, git's reflog of those refs under `logs/`, and what `local-canonical.yml` writes (`HEAD`, its config
keys, `hooks/update`). Anything else was put there by a person, or by something using the network service's uid
when the deploy is armed. So before you remove what a line names, look at when it appeared and whose it is
(`stat -c '%y %u:%g %a %n' <path>`): the service's audit log ([logging-architecture.md](logging-architecture.md))
for the same hours says what the service was asked to do; a change nobody asked for is the finding, not the file.
Where a file is the service's or root's, removing it takes `sudo`. What the tool's own git does about each of
these, and what that covers, is SECURITY.md C10.

| The line | It means | What to do |
|---|---|---|
| `config-key: <key>` | `config` holds a key that neither `git init --bare` nor the play writes. Some keys name a program or a path that git would use; the tool's own git sets its own answer for those (SECURITY.md C10) | standing in a directory that is NOT inside the canonical: `git config --file /srv/kontroll.git/config --get-all <key> \| cat -v` shows what it is set to (asked of a file by its path, git reads that one file and follows no include in it), and `git config --file /srv/kontroll.git/config --unset-all <key>` removes it — if nobody set it on purpose. A key the report shows with an escape in it (a backslash and `x`, `u` or `n`) cannot be typed back: open `config` in an editor |
| `config-value: <key>` | one of the keys the play sets, or `core.bare`, is missing from `config`, is there more than once, or is not set to what the play sets it to. Which value it has is not printed | for a key of the play, nothing: the same deploy sets it again, once (a dry run does not). It is expected ONCE, in the first deploy of a build whose play sets a key it did not set before. Otherwise somebody changed `config` since the last deploy: find out who (above). `core.bare` is the one the play does not set — standing outside the canonical, `git config --file /srv/kontroll.git/config --replace-all core.bare true` |
| `config-include: include.path` (or `includeif.….path`) | `config` pulls in a second config file. The audit did not read that file: its keys are not in this report | read the path it names (`--get-all include.path`, as in the first row); remove the include unless you put it there |
| `config-unread: config` | `config` is missing, is not a regular file (a second line — `link: config` or `not-a-file: config` — says so), cannot be read by the account that ran the audit, is larger than the audit reads or has a very long line, or git could not list it (it does not parse; git is not installed; a root that cannot become the service uid — §7) | `ls -l /srv/kontroll.git/config`. A config git cannot parse stops every git in the repository, the deploy's own included: it stops at "Harden the bare repo", after it has printed this line. A missing one is written again by that task, with the play's keys and nothing else — the next audit then names `core.bare` (the row above) |
| `hook: <name>` | a file in `hooks/` other than `update` and git's own `*.sample` files | remove it (as root: `hooks/` is root's) unless you installed it; the play installs `update` and nothing else |
| `hook-changed: update` / `hook-missing: update` | `hooks/update` is not the file this checkout ships (`ansible/playbooks/files/canonical-update-hook.sh`), or is not there | nothing for the hook itself: the same deploy installs it. It is expected once when an update brings a newer hook. If the NEXT deploy prints it again, something replaces the hook between deploys — or the checkout you ran the audit from is not the one the deploy runs from |
| `hook-off: update` | `hooks/update` is there and nobody may execute it. git runs no hook that is not executable, so while it is like this no push is checked by it | the same deploy sets its mode again; who cleared it is the question, and that took root |
| `hook-writable: hooks` / `hook-writable: update` | the directory, or the hook, is the network service's to write — its own, its group's with the group's write bit, or everybody's. The play makes both root's | the same deploy makes them root's again; find out who changed it — that, too, took root |
| `info-entry: <name>` | a file in `info/` other than `exclude` — `grafts` and `attributes` change what git reads; `refs` is a list git's own garbage collection wrote before the play told it not to | remove it unless you put it there. `refs`: remove it once; nothing reads it. If it comes back, look for a `config-value` line |
| `objects-info-entry: <name>` | a file in `objects/info/` other than `packs` — `alternates` makes git read objects from another directory; `commit-graph` (or `commit-graphs/`) is a cache of the history, which git's own garbage collection wrote before the play told it not to | `alternates`: remove it unless you set the canonical up to share objects — and if pushes arrived while it was there, objects may live only in that other directory: keep it until you have looked. `commit-graph`: remove it once (the tool does not read it; removing it loses nothing). If it comes back, something writes it |
| `branches-entry: <name>` | a file in `branches/`, git's old place for a remote's shorthand (a URL a fetch or push from this repository would use) | remove it; the tool sets up no remote in the canonical |
| `top-entry: <name>` | an entry of the top directory that a canonical made by the play does not have (`commondir`, `shallow`, `worktrees`, `gitdir`, a `.lock` file a git that was killed left behind, `gc.pid` while git collects garbage, a stray file) | `commondir` and `shallow` change which objects and refs git reads: remove them unless you know why they are there |
| `reflog: logs/<path>` | a file under `logs/` that is not git's reflog of `HEAD`, of `refs/heads/main` or of a proposal | remove it |
| `ref: <name>` / `packed-ref: <name>` | a ref other than `refs/heads/main` and `refs/heads/proposed/<run id>` — a tag, a `refs/replace/…`, another branch, a proposal whose id the service does not mint, a `.lock` file a git that was killed left beside a ref. The tool reads none of them: it names its own two kinds in full | remove it unless you made it: its file under `/srv/kontroll.git/refs/`, or its line in `packed-refs` — a text file, one ref a line; under an annotated tag's line there is a second one that begins with `^`, which goes with it. With an editor, not with git. The Pending panel lists the ones under `proposed/` as "not proposals" |
| `symbolic-ref: <name>` | a ref that names another ref instead of a commit | as above; the tool refuses to promote or discard it |
| `head: HEAD` | `HEAD` does not name `refs/heads/main` (a fresh clone would check out something else), or is not there (git then takes the directory for no repository at all) | nothing for the file itself: the same deploy writes it. Who changed it is the question |
| `link: <path>` / `not-a-file: <path>` | `link`: a symbolic link where a file or a directory is expected. `not-a-file`: something else that is not what belongs there — a pipe, a socket, a device, a file where a directory belongs or the reverse. The audit did not follow or open it | find out who made it; remove it |
| `unread: <what>` | the audit says nothing about that part: it could not be listed or read by this account; it is larger or deeper, or holds more entries, than the audit reads; a line of `packed-refs` is one git would not take for a ref (git refuses the whole file for it — or it is the `^` line left behind when a tag's line was removed by hand); more lines of one kind than are shown; or the audit ended on an error of its own, and what it printed is not a report | look at that part yourself — with `sudo` where your account may not read it. For the last one, run the audit again by hand |
| `note: FETCH_HEAD` (`ORIG_HEAD`, `gc.log`) | a file git leaves in the top directory by itself | nothing |

### The ownership pass named something, or the deploy stopped at it

The same script's other mode is what the play applies to the canonical a little later: `python3 -B
scripts/kontroll-canonical.py --normalise /srv/kontroll.git --owner <uid> --group 1001` (the tasks "Detect canonical
entries outside hooks/ …" and "Make existing canonical entries operator:1001 …"). It makes every entry outside
`hooks/` the operator's, group 1001, group-writable and, a directory, setgid — so the operator's push and the
service's write land beside each other (F-CANON). It walks the canonical as the audit reads it and changes each
entry through the descriptor of the thing it opened: a symbolic link is named and left as it is, and nothing it
points at is touched. The detect task runs `--check`, under the dry-run too, and prints what the apply task then
changes — names only, bounded per kind. A canonical whose entries are already as asked prints nothing, and the
apply task is skipped. To run it by hand, run the detect form first, as root, from the checkout; the exit status is
3 when something would change, 0 when nothing would, 1 when a part was not gone over (the lines say which).

| The line | It means | What to do |
|---|---|---|
| `owner: <path>` / `mode: <path>` | the entry was not `<operator>:1001`, or lacked the group's write bit (a directory: or the setgid bit) — and was changed by the apply task (the detect task: would be) | nothing. Expected once for a canonical made before the grant, and for what the service wrote while a deploy was armed (its proposals: `refs/heads/proposed/…`, objects). If the same name comes back on every deploy, something resets it between deploys |
| `link: <path>` | a symbolic link where the pass expected a file or a directory. It was left as it is, its own owner too, and what it points at was not looked at | the tool makes no link in the canonical: find out who did (`stat`), and remove it unless you put it there. The audit above names it too |
| `not-a-file: <path>` | a pipe, a socket or a device: named, never opened | as above |
| `unread: <path>` | a part the pass did not go over: a directory it could not list or open, an entry it could not open, a tree with more entries or more levels than it goes. The deploy stops at the detect task (`--check`); a line of the form ``unread: N more `mode` lines are not shown`` is a count, not a part, and stops nothing. An entry git itself removes during a push (`*.lock`, `tmp_obj_*`) is not one: a part that is gone is nothing to change | look at that part yourself, as root. `<directory> (more entries than this reads)` is that directory holding more than 20 000 entries; `the top directory (more entries than this reads)` is a canonical with more than the pass goes over in one run (4 194 304) |
| `failed: <path>` | the kernel refused the change — an immutable file (`lsattr`), a read-only file system. The deploy stops at the apply task | `chattr -i <path>`, or mount the file system writable, and run the deploy again |
| the task fails with "nothing was changed" on stderr | the pass could not start: no `python3`, a path that is not a directory, `--owner`/`--group` not numeric — or a platform without descriptor-relative operations (the pass refuses on Windows rather than change anything by path) | run the deploy on the control node, from the checkout |

## 9. A content clone was made again, or its update failed

The deploy makes and updates the API's and onboard-gui's content clones (`<storage root>/api/repo`,
`<storage root>/onboard-gui/repo`) with `ansible/playbooks/tasks/content-clone.yml`: as root when the deploy is
unarmed, as uid 1001 when it is armed (`api_privileged`); a clone held by the wrong uid is removed and made again
([SECURITY.md](../SECURITY.md) C10).

| What you see | What it means | What to do |
|---|---|---|
| `<path> must be a directory of root's that neither its group nor anybody else may write, and that anybody may pass through …` | the clone's parent (`<storage root>/api`, or onboard-gui's) or the storage root is not root's alone, or others may not pass through it (the armed git, uid and gid 1001, passes by the bit for others). The deploy makes each parent root's before it gets here, and a missing storage root root's `0711`, so something changed it back, the storage root you chose is another account's, group-writable or closed to others (`0700`, `0750`), or an older build made it: its first store made it its own (10002's or 1001's `0700`, 10001's `0750`, 1001's `0755`). In a `--check --diff` it can also be a parent the previous build left the service's (1001's `0755`): a dry-run changes nothing, so the first one on a box an older build deployed stops there, armed or not | as the message says: `sudo chown root:root <path>` and `sudo chmod 0711 <path>`, and deploy again. For a parent named by a dry-run, the `chown` alone is enough (it is what the deploy does there first; the parent's `0755` passes): run it and dry-run again — the CHANGELOG's 2026-10-14 entry, "On a box that runs the previous build". A relocated root needs every directory above it passable by others too (`ls -ld` each): the refusal does not look at them, and a closed one stops the armed git at `could not create leading directories` |
| "Remove the clone — it is not wholly its owner's …" reports `changed` | the clone was held by the wrong uid for this deploy: the service's (or part of it) on an unarmed deploy; not the service's, or not a directory, on an armed one. The first deploy after arming or disarming does this once | nothing: its owner made it again and its service was restarted. The next deploy leaves it |
| `… is not a git clone: remove it and run the deploy again` | the armed clone's directory is the service's but holds something that is not a clone (a clone that stopped part-way, or files put there by hand) | `sudo rm -rf <storage root>/api/repo` (or onboard-gui's) and deploy again |
| the removal fails — `rmtree failed: Cannot call rmtree on a symbolic link` (Python 3.11, the installer's), `rmtree failed: [Errno None] None: '<path>'` (Python 3.12), or another `rmtree failed: …` such as `[Errno 39] Directory not empty` | a directory of the clone was exchanged, or written into, while it was being removed; only a writer of the clone can do that: the service's uid | stop the service (`docker compose stop api`, or `onboard-gui`), deploy again, and find out what was writing |
| the armed update fails `detected dubious ownership in repository at '/srv/kontroll.git'` | uid 1001's git did not read `<storage root>/content-clone.gitconfig` (since git's May-2024 security releases — 2.45.1, and 2.39.4 and the other maintenance lines — git refuses to serve a repository another uid owns unless it is named safe) | the same deploy writes that file before it runs git: check it is there, root's, mode `0644`, and that uid 1001 can reach it. On the first `--check` after an upgrade the file is not there yet: the preview says `main not compared` |
| the armed update or its preview fails `cannot read refs/heads/main from file:///srv/kontroll.git` | uid 1001 cannot read the canonical | `local-canonical.yml` gives group 1001 its read and write grant: run the deploy that runs it, then this one |
| the unarmed update fails `Local modifications exist in the destination: <storage root>/api/repo (force=no).` | a tracked file of the root-owned clone was changed — only root can have done it | look at it (`sudo git -C <storage root>/api/repo status` — the clone is root's), then `sudo rm -rf <storage root>/api/repo` and deploy again |
| the armed update says `a change made in the clone and not proposed is discarded` | the service changed a tracked file in its clone and did not propose it; the update puts the clone back at the canonical's `main` | propose the change: a proposal is pushed to the canonical, not kept in the clone |

## 10. A landing was refused, or did not happen

Every way the operator's or the deploy's commit reaches the canonical's `main` is a LANDING
(`scripts/kontroll-land.py`; `gitio.land_commit` — SECURITY.md C10): `scripts/update.sh`,
`scripts/update-from-bundle.sh`, an onboard or a capture-exception from the CLI (`scripts/galaxy.py … --commit`),
the deploy's `local-canonical.yml`, and the GUI run from a checkout. A landing reads the canonical's `main` once,
refuses to discard what it holds, judges the worktree's commit with the promote gate, brings it across as a pack
and moves `main` in one compare-and-swap. It prints one `kontroll_run_id=… land …` line first. Exit 0 landed (or
`up to date`); exit 2 REFUSED by the gate, or the gate could not run; exit 1 nothing to land as asked. After any
exit but 0 the worktree keeps its commit and the canonical is untouched.

| What you read | Where | What it means | What to do |
|---|---|---|---|
| `REFUSED: the tree does not resolve, so the next bootstrap … would stop: …` and the generator's sentence (`conflict`, `signature_policy`, `enabled_modules entry N`, `the tree holds a symbolic link …`) | `kontroll-land.py` (exit 2); `update.sh` / `update-from-bundle.sh` (`!! The landing did not happen (exit 2)`); the deploy's `local-canonical: REFUSING to land (exit 2) — …`; the CLI's `! the landing on the local canonical did not happen (rc=2)` | the commit's tree is one the promote gate refuses — the same rules as §6 and §7, judged at the door now: before this build such a tree was mirrored and then refused every promote | correct it in the checkout (the sentence names what: a fleet line, two units pinning one collection differently, a fleet-wide `signature_policy`, a link), commit, and run the same command again. The code of an update is merged in the checkout already; only the canonical lags. `python3 scripts/kontroll-land.py --skip-conflict-check` lands WITHOUT the gate — the human's override, for a node that must move while the gate cannot run, or a tree you have read yourself |
| `REFUSED: the gate could not run, so the tree was not judged: …` | the same | this checkout has no `scripts/gen-requirements.py` or `scripts/kontroll-promote.py`, the generator could not start or did not finish, or the worktree's repository could not be read | as the sentence says; `--skip-conflict-check` is the override, as above |
| `kontroll-land: the canonical (…) has commit(s) your worktree (…) does not, so this landing would discard them …` | the same, exit 1 | a promote advanced the canonical's `main` with a commit this worktree has never fetched, or the worktree is on a commit that is not a descendant of `main` | `git -C <worktree> fetch local && git -C <worktree> rebase local/main`, then run again. Never `--force` anything: the canonical is where promotes land |
| `kontroll-land: refs/heads/main moved while this landing was being judged …` | exit 1 | another landing or a promote moved `main` between this landing's read and its transaction; git refused the compare-and-swap | run it again |
| `kontroll-land: the commit is not whole in the canonical after the pack was received …` | exit 1 | the pack did not carry every object the commit names (a worktree that is itself missing objects — a shallow or damaged clone) | `git -C <worktree> fsck`; land from a whole clone |
| `kontroll-land: the worktree … has no \`local\` remote naming a canonical on this machine …` | exit 1 | the checkout has no `local` remote, or it names something that is not a directory here (an `ssh://` URL) | `ansible-playbook ansible/playbooks/local-canonical.yml` sets it; or `--repo /srv/kontroll.git` |
| `kontroll-land: refs/heads/proposed/<id> exists already: a proposal is created once` | `--propose`, exit 1 | that run id is taken | mint another id |
| `remote: C10: refused refs/heads/main — refs/heads/main is never pushed, by anyone …` | a `git push` by hand into the canonical | the canonical's hook refuses a push of `main` from everyone: nothing legitimate pushes it | `python3 scripts/kontroll-land.py` from the checkout (`--check` first to see what would land) |
| `local-canonical: the operator (…) has uid 1001 …` | the deploy, first task | the operator's uid is the network service's; the canonical's grant and hook cannot tell the two apart | deploy as an account with another uid |

## See also
- [logging-architecture.md](logging-architecture.md) — the layered design + the degrade-path source of truth
- [../SECURITY.md](../SECURITY.md) **C22** — the request boundary, the refusal that is the repository's own, and a
  fleet line that is not a class key
- [SETUP.md](SETUP.md) §3 — the fleet file
- [observability/logging-capability-vector.md](observability/logging-capability-vector.md) — the logging capability
- [../SECURITY.md](../SECURITY.md) **C12** — logs are a secret surface (label hygiene, mgmt-only, at-rest)
- `dashboards/grafana/dashboards/logs.json` — the same signals as a Grafana pane
