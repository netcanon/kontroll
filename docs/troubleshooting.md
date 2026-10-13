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
{source=~".+", level=~"error|warn"}

# One device class's ingested logs (the logging capability):
{source="capability", device="cisco_ios"}

# Free-text body search (a |~ filter — a query, never a label, so no cardinality cost):
{source="internal"} |~ "(?i)backup.*fail|captured nothing"

# Volume of a chatty emitter (sizing-lever input; pairs with the dashboard "Top services" panel):
topk(10, sum by (service) (count_over_time({source="internal"} [1h])))
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
| `local-canonical: REFUSING to commit — gen-requirements: instance/fleet.yml: …` | a deploy (its canonical play, before the stack play), also the dry-run | the deploy's own commit-and-mirror would have put the line on the canonical's `main`, where it refuses every promote; nothing was committed or pushed to the canonical | the same; then deploy again |
| `REFUSED: promoting proposed/<run_id> — the proposal's tree does not resolve …`, and under it the generator's sentence, `gen-requirements: refusing to resolve the collection set — instance/fleet.yml: enabled_modules entry N …` | the `promote-proposal` job, or `kontroll-promote.py` | the tree the promote would make `main` holds such a line. If the message adds that `main` as it stands is refused as well, the line is on `main` already — and then EVERY proposal is refused, whatever it changes | if the line is in the node's checkout (a hand edit there is how it usually reached `main`): correct it there and deploy — the deploy's commit-and-mirror takes the correction to `main`. Otherwise promote a proposal that removes or corrects it: the Settings view's `disable` control stages the removal of a line that is plain, unquoted text; for any other line (`no`, `~`, a number, a quoted name, one with a slash) make the proposal by hand — in a clone of the canonical correct `instance/fleet.yml`, commit, `git push <canonical> HEAD:refs/heads/proposed/<twelve lowercase hex digits>` as the operator, and promote that id; then bring the node's checkout forward (`git fetch local && git rebase local/main`) before the next deploy. A proposal staged before the correction still carries the old line and is refused with the same sentence: discard it and propose again |
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

## See also
- [logging-architecture.md](logging-architecture.md) — the layered design + the degrade-path source of truth
- [../SECURITY.md](../SECURITY.md) **C22** — the request boundary, the refusal that is the repository's own, and a
  fleet line that is not a class key
- [SETUP.md](SETUP.md) §3 — the fleet file
- [observability/logging-capability-vector.md](observability/logging-capability-vector.md) — the logging capability
- [../SECURITY.md](../SECURITY.md) **C12** — logs are a secret surface (label hygiene, mgmt-only, at-rest)
- `dashboards/grafana/dashboards/logs.json` — the same signals as a Grafana pane
