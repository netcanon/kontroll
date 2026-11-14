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
| Container stdout/stderr (every stack service) | `internal` | the **container name**¹ (`vector`, `loki`, `semaphore`, `kontroll-api`, …) | from the Docker source |
| Host journald (systemd) | `internal` | the **unit**² (`docker.service`, `sshd.service`, …), else `host` | the persistent journal, else the volatile one³ |
| API audit (who/when/what on the API) | `internal` | `api-audit` | kept **90d**; never the credential |
| GUI audit (onboard-GUI actions) | `internal` | `gui-audit` | kept **90d** |
| Ansible run-log (playbook runs) | `internal` | `ansible` | carries the `run_id` of the run |
| Script run-logs (CLI helpers) | `internal` | the **script name**, else `script` | from `~/.local/state/kontroll/runs` |
| A device/service's own logs (logging capability) | `capability` | the **method** (`syslog_push`, …) | + `device=<module key>` (e.g. `cisco_ios`) |

¹ A compose one-off is labelled without its run hash: the installer's `services-init-run-<hash>` is
`services-init` (an `init` or `check` run; its other verbs, `fresh-init` and `shell` among them, keep no log). A
bare `docker run` with no `--name` gets a random name, and so a stream of its own. A container named
`kontroll-throwaway-…` (a deploy's short helper, where an edition runs one) is not shipped at all: the source
excludes the prefix (I-1), and the deploy's own output carries what it prints.
² Less what systemd mints per login, run, container scope or instance: `session.scope`, `run.<type>`,
`docker.scope`, and every template's instance dropped (`getty@.service`, `wg-quick@.service`). The journald
capability methods fold the same way. The full name stays in the body — pick one with
`{source="internal", service="services-init"} | json | container_name="services-init-run-<hash>"` or
`… | json | _SYSTEMD_UNIT="wg-quick@wg1.service"`.
³ deploy-stack binds `/var/log/journal` where the control node has it, else `/run/log/journal`, and its log says
which ("Vector reads the host journal at …"). A volatile journal holds the current boot only. After the control node
first keeps a persistent journal (roles/logging_journald_remote sets `Storage=persistent` and flushes the journal to
`/var/log/journal`), re-run deploy-stack so Vector reads it. `kontroll_journal_dir` (in the control node's inventory,
or `-e`) overrides the host's answer: `/run/log/journal` is for a node set to `Storage=volatile` in journald.conf that
still has an old `/var/log/journal` (on a node whose journald persists, Vector would read a directory each boot
empties); `/var/log/journal` is refused where the host has none, since binding it would make it. A node where the
bind before I-25 made `/var/log/journal` keeps a persistent journal, and Vector now reads it. To go back: run
`journalctl --relinquish-var`; move `/var/log/journal` aside (since the flip it holds the only copy of the journal);
re-run deploy-stack at once, which binds Vector to `/run/log/journal`; then restart journald. Removing the directory
alone is undone: a Vector container still bound to it makes it again at its next start.

## 2. Common LogQL queries (Grafana → Explore → Loki, or `logcli`)

```logql
# Everything from one operation, greppable by its correlation id (internal only):
{source="internal", run_id="20260616T143000Z"}

# A specific surface (audit trail kept 90d):
{source="internal", service=~"api-audit|gui-audit"}

# Anything wrong, across the whole stack:
{source=~".+", level=~"(?i)(warn|warning|err|error|crit|critical|alert|emerg|fatal|panic)"}

# The same, leaving out blackbox-exporter (a target that does not answer logs two lines per probe; see §4):
{source=~".+", service!="blackbox-exporter", level=~"(?i)(warn|warning|err|error|crit|critical|alert|emerg|fatal|panic)"}

# Which blackbox targets do not answer (each failed probe logs `Probe failed`, an error):
sum by (target) (count_over_time({source="internal", service="blackbox-exporter"}
  | json message | line_format "{{.message}}" | logfmt | msg="Probe failed" [1h]))

# One device class's ingested logs (the logging capability):
{source="capability", device="cisco_ios"}

# Free-text body search (a |~ filter — a query, never a label, so no cardinality cost):
{source="internal"} |~ "(?i)backup.*fail|captured nothing"

# Volume of a chatty emitter (sizing-lever input; pairs with the dashboard "Top services" panel):
topk(10, sum by (service) (count_over_time({source="internal"} [1h])))

# What the "Container crash storm" alert counts, by container (its rule sums the same over 5m). Run it
# when the alert fires, and over the last 7 days before changing the rule: Grafana logs the rule's uid at every
# evaluation and each deploy prints `TASK [Restart …]` banners, so a looser filter counts lines that are no crash.
sum by (container_name) (count_over_time({source="internal"} | json container_name, message | __error__=""
  | container_name != "" | line_format "{{.message}}" |~ "(?i)restart|panic|exited"
  !~ `kontroll-log-container-restart|^(TASK|RUNNING HANDLER) \[` [5m]))
```

A failed delivery's error and retry lines are container lines too: they name the alert's title and folder, the
contact point, the matchers of the policy route it took (the heartbeat's `{kontroll="heartbeat"}`), any label that
route groups by, and the notifier's host (Vector keeps a URL's scheme and host). No string in the alerting files but
the crash rule's own uid and query says a counted word (`tests/unit/test_alerting_metrics.py`), but a notifier host
whose name holds one, or a label value a route groups by (a host's name), counts its own failed deliveries.

## 3. The degrade path — debugging when Loki/Vector is down

Observability must survive an observability outage. Loki or Vector being down loses log **shipping**, never the
source logs — every surface persists locally and is readable without the stack. Per the access-chain header in
`docker/services/vector.yaml` ("Fallback required: no … degrade to `docker logs`/journalctl/the flat audit files"):

| You want… | When the stack is up | When Loki/Vector is down |
|---|---|---|
| A container's logs | the `internal` stream by container name (a one-off: less its `-run-<hash>`) | `docker logs <name>` (bounded by daemon.json `max-size 10m ×5`; the installer's only while an `init` or `check` run lasts: `--rm` removes it with the container, and its other verbs keep none) |
| Host/systemd logs | the journald `internal` stream | `journalctl -u <unit>` on the control VM |
| API audit | `{service="api-audit"}` | the flat TSV at `/var/lib/kontroll/api/audit/` |
| GUI audit | `{service="gui-audit"}` | the flat TSV at `/var/lib/kontroll/onboard-gui/audit/` (M11 — off the `/repo` code/propose clone) |
| Ansible run-log | `{service="ansible"}` | the flat log at `/var/lib/kontroll/ansible-log/` |
| Script run-logs | `{service=<script>}` | `~/.local/state/kontroll/runs/` |

Bring the stack back: `docker compose ... up -d loki vector` (or re-run `deploy-stack.yml` with `loki`/`vector`
in `stack_services`). Vector resumes from its on-disk checkpoint (the `vector-data` volume), so a restart
re-ships from where it stopped rather than re-reading everything. Every deploy, with Vector in it or not, writes
the journal Vector binds and its group into `docker/.env` (`KONTROLL_JOURNAL_DIR`, `KONTROLL_JOURNAL_GID`).
Run a hand `up` from a shell that does not export either: compose takes a shell's value over `--env-file`.

A new build's Vector transforms or Grafana alert rules not in effect after a deploy: Vector, Grafana, Loki,
Prometheus, Caddy, the exporters and the GUI (its age key) read their bound config only at start, and the deploy
restarts each one whose config changed since it loaded it (`restarting, for config changed since loaded: …` in
the run); a file under the storage root, such as that key, changes with its mode or owner too,
since a file bind keeps the file the container started with. One the run did not name had no change on record:
`docker compose restart <service>` loads the tree as it is (the next deploy may restart it once more, harmlessly).
A config the service reloads by itself (Grafana's dashboards, Prometheus's file_sd targets) restarts nothing; see
`scripts/stale-config.py`.

## 4. Quick checks

- **No logs in Grafana at all** → confirm the Loki datasource provisioned (`uid: loki`); confirm Vector is
  Up and shipping (`docker logs vector | grep -i "Listening\|Healthcheck"`); confirm Loki `/ready`
  (`curl -s ${KONTROLL_MGMT_IP}:3100/ready`).
- **Vector logs `Error in communication with Docker daemon` about every ten minutes** → the socket proxy is
  cutting idle log streams: its log-stream route is missing. `docker logs docker-socket-proxy | grep kontroll:`
  says why (Vector excludes the proxy, so its WARNING never reaches Loki). Now and then the error is real:
  `timeout client 10m` still cuts a stream Vector stopped reading, e.g. while Loki was down.
- **No container logs, and `docker-socket-proxy` keeps restarting** → its start script could not add the
  container read limit and refused to start (it fails closed). `docker logs docker-socket-proxy | grep kontroll:`
  shows the ERROR. The usual cause is a full disk (the copy the script writes comes out empty or short, and it is
  not moved in), or an image whose template has no `http-request allow` line. Once the disk has room, the next
  restart adds the limit and starts the proxy. On a disk with a page or two to spare, the script's edits go in and
  the image's own entrypoint fails instead: `grep kontroll:` shows the limit line (or "already present"), then the
  route's WARNING, then `sed: write error`. Same cause, same cure.
- **The error/warn panel is full of blackbox-exporter lines** → a probe target does not answer: each failed probe
  logs `Probe failed` (error) and `Timeout reading from socket` (warn). §2 has the query that names the targets,
  and an "Anything wrong" query that leaves blackbox out. A device that is gone comes out of the inventory (its
  probe target goes with it); a box run from the example inventory probes its TEST-NET addresses, which never
  answer.
- **A device's syslog never arrives** (`{source="capability"}` empty) → confirm `docker/services/vector.yaml`
  publishes the mgmt-bound `:5514`, the device is configured to ship there (the `logging_syslog_push` wiring
  role), and the device can reach the collector on the mgmt VLAN.
- **`ping.yml` reports a host `REFUSED: its class has no check entrypoint installed`, and the run fails** → the
  host's `device_role` is missing or empty, or the first role of that name where Ansible looks for roles holds no
  `tasks/check.yml` (`.yaml`, `.json` or a bare `check` too) with a task in it (an empty or comment-only file counts
  as none); the refusal's own message, earlier in the run's output, names the class and the roles path searched. A
  role of that name earlier on the path shadows a later one that holds the file. The other hosts are probed and
  checked as usual, and the run's SSH keys are still removed. Fix the host's `device_role` in its inventory drop-in,
  or give its class's role a `tasks/check.yml`. If the run's output shows a read error at that task instead, the
  run's user cannot read the file.
- **`backup-configs.yml`'s summary reads `FAILED: the capture stopped at the task "Refuse a host whose class has no
  entrypoint for this play installed"`** → the same check, for `tasks/backup.yml`: the host's class has no backup
  entrypoint installed (as above, with `backup` for `check`). The summary's "(a no_log task prints only that it
  failed)" is its general wording: this task is not no_log, and its own message, earlier in the run's output, names
  the class and the roles path searched. The other hosts' captures go on, and the run's SSH keys are still removed.
- **`wire-logging.yml` fails with `the wiring request was not validated, so no host was wired`** → the run's
  `--limit` leaves out localhost, where the validation play runs (or its `--tags` left out the validation's tasks), so
  no host was wired. Run it again with `--limit <host>:localhost`, or with no limit to wire every host of the class;
  where `--tags` left the validation out, run it without `--tags`. The run's SSH keys are removed as usual, in a
  `--tags` or a `--check` run too (I-93, I-87).
- **`wire-logging.yml` passes and its `Wiring run report` reads `<host>: syslog-push: not run — unreachable: the
  device took no TCP connection on port <n>`** → the device did not answer on its SSH port (down, no route, a
  firewall, or a wrong `ansible_port`: a port set only in an SSH config is not the one probed), so nothing was
  pushed to it; the other devices were wired. Fix its reachability and wire it again (`--limit <host>:localhost`).
  Where its summary reads `FAILED` at `Fail a push to a device that took the connection or sits behind a jump host`
  instead, the device answered and the push failed: that task's message, earlier in the output, says whether its
  port took the connection (its credential, or a line it refused) or it sits behind a jump host, which no probe
  reaches (I-103).
- **`install-node-exporter.yml` passes and its `Install run report` reads `<host>: not installed: its fact gathering
  was unreachable`, or `<host>: its install may not be done: it answered the fact gathering, then not the re-check
  after its install`** → the host was down or refused the run's connection (its key, its host key), so nothing was
  installed on it; or it answered first and stopped answering during the install, each task it did not answer
  ignored, so node_exporter may be missing or stopped there. The other hosts were installed. Fix its reachability and
  run the install again (`-e target=<host>`; a host that answers installs, and a second run reports `0 changed`). A
  host that stopped answering and answered again before the re-check is not named: its recap counts each task it did
  not answer as `ignored` (I-92). Under a caller's `any_errors_fatal` (`ANSIBLE_ANY_ERRORS_FATAL`) such a host stops
  every host instead, and the run fails before the key cleanup, its rescue's message naming the host's own
  unreachable gathering or re-check (I-97).
- **A fleet run (`backup-configs.yml`, `ping.yml`, `install-node-exporter.yml`, `wire-logging.yml`) started with
  `--start-at-task` passes having run nothing, and prints `[ERROR]: No matching task "<name>" found. Note:
  --start-at-task can only follow static includes.`** → ansible-core never met the named task, so it ran no task
  anywhere: no capture, check, install or wiring, and no key cleanup (the run rendered no keys either; a key dir an
  earlier run left stays). The name is of a task inside a role (a class's entrypoint, or the role a fleet play
  includes, reached only through a dynamic include), of an `import_tasks` (an import is replaced by its tasks, so its
  name is no task's), or, in `wire-logging.yml`, of any task after the validation play (its later plays target no host
  until the validation names the group). Start at a task of the playbook's own plays (in `wire-logging.yml`, a
  validation task), or run it whole. A start past the play that does the work reads results no task set:
  `ping.yml`'s summary fails every host, `install-node-exporter.yml`'s include fails every host (its gathering
  skipped), and a start past the key cleanup passes having done nothing (I-99).
- **`wire-logging.yml` passes and its `Wiring run report` reads `<host>: not wired: it did not answer the probe
  before its wiring`, or `<host>: its wiring may not be done: it answered the probe before it, then not the re-check
  after it`** → an ssh host (journald-remote, file-tail-ssh) was down or refused the run's connection (its key, its
  host key), so its wiring role was not run; or it answered the probe and stopped answering during the wiring, each
  task it did not answer ignored, so its journald or its read key may not be set there. The other hosts were wired.
  Fix its reachability and wire it again (`--limit <host>:localhost`; a second run reports `0 changed`). A host that
  stopped answering and answered again before the re-check is not named: its recap counts each task it did not
  answer as `ignored` (I-92). Under a caller's `any_errors_fatal` such a host stops every host instead, and the run
  fails before the key cleanup, its rescue's message naming the host's own unreachable probe or re-check (I-97).
- **A fleet run fails at `Refuse a key dir that holds anything but SSH keys` with `kontroll_device_key_dir (…) holds
  more than SSH keys: …` or `… is not a directory`** → the dir the run renders its SSH keys into, and removes whole
  after, holds something else (the names listed), or the path is a file or a link. Most often
  `$KONTROLL_SSH_KEY_DIR` names a dir of the operator's (a home dir, `~/.ssh`, `/tmp`): point it at a directory of
  its own, or unset it to use `kontroll-ssh-keys.<run id>` under `$XDG_RUNTIME_DIR` or `/tmp`. The path is kept as
  it was; nothing in it is changed or removed. The render refuses each key-auth host there before it writes a key;
  where a host is left (a password-auth one), the cleanup refuses the path again and fails every host, and the run
  ends there. A dry run (`--check`) uses the path with `.check` appended, so a dry run and a real run name different
  dirs (I-95, I-87).
- **`ping.yml` reports a host `UNREACHABLE` that answers otherwise** → the probe is a TCP connect from the control
  node, and its port is the first set of `mgmt_port`, `ansible_port`, `kontroll_probe_port`, then 22
  (`ansible/playbooks/ping.yml`'s header). An SSH host on another port needs its port typed at onboard (it is
  written as `ansible_port`). An `api` host onboarded before S28 is probed on 22 until it has
  `kontroll_probe_port: 443`: add that line to the host in its drop-in, or re-onboard it WITH its token — a
  re-onboard replaces the host's vars, and a blank token box drops its token var, so the check then fails with
  "no API token is in scope". A host that already sets `ansible_port` (say, a group_vars `all.yml` for SSH on
  another port) is probed on it, api hosts included: give those `mgmt_port`. For the probe, a port var set to null
  or empty counts as unset (Ansible's own connection still refuses an empty `ansible_port`). The pre-flight, on the
  control node with no device contacted, prints each host's port before and after S28 — every host onboarded before
  it should read `N -> N` (the single quotes keep the message one value):
  `ansible -i instance/inventory all -m ansible.builtin.debug -a "msg='{{ mgmt_port | default(22) }} -> {{ mgmt_port
  | default(ansible_port | default(kontroll_probe_port | default(22, true), true), true) }}'"`.
- **`ping.yml` reports a host `CHECK FAILED: its check stopped at the task: …`, and the run fails** → its port
  answered, and its class's check failed at that task: the task's own error, earlier in the run's output, says why
  (a task that handles a credential is `no_log`, and on the shipped runtimes prints only that it failed, so its name
  is the clue; SECURITY.md's I-75 row names where ansible-core prints more). For an API device, most often its
  credential is missing from its SOPS domain or rejected by the device; a network-CLI device whose SSH login is
  rejected reads CHECK FAILED too, at its `cli_command` task. The other hosts are checked as usual, and the run's SSH
  keys are still removed. In a dry run (`--check`) the line ends `(dry run: the port was not probed)` and the run
  does not fail for it: a dry run skips the probe and runs each check in check mode, where a check can stop at a
  read it does not make; an `OK (dry run: the port was not probed)` says nothing of the port either, and a dry run
  given `-e kontroll_ping_require_reachable=true` is refused, every host failed by name, since it cannot prove one
  reachable. Before I-84 such a host read `UNREACHABLE` and the run passed. A host with no summary line at all, the
  run ending rc 4, is a raw-SSH host whose check could not connect (an SSH login or banner failure): the run's output
  names it.
- **A script prints `run-log: cannot make …` or `run-log: cannot write … continuing without a run-log`, a launcher
  prints `kontroll: cannot make ~/.local/state/kontroll/runs`, or `~/.local` is root's** → Docker made the run-log
  directory Vector binds (`~/.local/state/kontroll/runs`) and its missing parents, as root, at an `up` before anything
  had made them (I-23; the launchers and a host deploy now make it first, as the operator), or a script run as root
  with your HOME (`sudo -E`, `su -m`) made them. A host deploy since then has given `runs`
  back; on a node run through the launchers it, like its parents, stays as Docker left it. Check with
  `ls -ld ~/.local ~/.local/state ~/.local/state/kontroll ~/.local/state/kontroll/runs`, and give back each one that
  is not yours (root's, or a number where a userns-remapped Docker made it):
  `sudo chown "$(id -u):$(id -g)" <that directory>`.
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
| a device class and `role:` (`modules/<key>/module.yml`) | the class's module file names no role, and the backend it names declares none (`vendor_config` and `napalm` ship without one) or is not one this install ships; or it names `backend_vendor_config` / `backend_napalm`, the role kontroll made up before S26 (CHANGELOG, Upgrading); or a backend registry entry's own `role` is not a name | give the class a `role:` in its module file, or onboard naming a backend that declares one; correct a registry entry's `role` that is not a name (`ansible/backends/<name>/backend.yml`) |
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
| `REFUSED: promoting proposed/<run_id> — the proposal's tree does not resolve …`, and under it the generator's sentence, `gen-requirements: refusing to resolve the collection set — instance/fleet.yml: enabled_modules entry N …` | the `promote-proposal` job, or `kontroll-promote.py` | the tree the promote would make `main` holds such a line. If the message adds that `main` as it stands is refused as well, the line is on `main` already (a deploy of an older build mirrored it unjudged) — and then EVERY proposal is refused, whatever it changes | if the line is in the node's checkout (a hand edit there is how it usually reached `main`): correct it there and deploy — the deploy's commit-and-landing takes the correction to `main` (the landing judges the corrected tree, not `main` as it stands). Otherwise promote a proposal that removes or corrects it: the Settings view's `disable` control stages the removal of a line that is plain, unquoted text; for any other line (`no`, `~`, a number, a quoted name, one with a slash) make the proposal by hand — in the node's checkout (or a clone of the canonical with a `local` remote) correct `instance/fleet.yml`, commit, `python3 scripts/kontroll-land.py --propose <twelve lowercase hex digits>` (it judges the tree first and creates `refs/heads/proposed/<id>`; never `git push` — the canonical's hook refuses `main` from everyone and the tool pushes nothing by hand), and promote that id; the next deploy brings the node's checkout forward itself (`kontroll-land.py --sync`). A proposal staged before the correction still carries the old line and is refused with the same sentence: discard it and propose again |
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
(`sudo find -H /srv/kontroll.git -maxdepth 1 -printf '%u:%g %m %p\n'` shows the top level; `-H` follows a link at
the path, as the play does); it does not look into
the object store. A canonical that holds only what the play and the tool write prints nothing. An empty directory
at the path is no canonical and is not audited: it is what Docker leaves when the installer binds a
`/srv/kontroll.git` that is not there yet, so a first install through the installer has one, and its dry run says
the real run will seed the canonical. A directory that holds entries but no `HEAD` is audited, and the dry run says
the same of it: the root of a filesystem mounted at the path holds `lost+found`, which the audit names
(`top-entry: lost+found`) on that deploy and every later one, since the play leaves an entry it did not make; the
dry run's ownership pass names it too, as the real run makes it yours and gid 1001's, group-writable. A repository
whose `HEAD` is gone (`objects` or `refs` there, and no `HEAD`) is audited (`head: HEAD`), and its dry run says that
the real run restores `HEAD` and then judges the landing, which refuses it if the canonical holds a commit your
checkout lacks. A symbolic link at the path is followed: the canonical is the directory it names, and each case
above is of that directory. A link to nothing is no canonical yet, and the real run makes the directory it names
and seeds a canonical there, making every directory missing on the way to it as the canonical's own (yours and gid
1001's, group-writable); so a link into a disk that is not mounted gets a new canonical under the mount point, on the
disk the mount point is on, seeded with your checkout's HEAD and none of the history promoted on the unmounted disk.
Mount the disk before the deploy. A link on the host is not one inside the installer: its bind mount takes the
directory the link names, so a deploy through the installer sees that directory at the path.

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
| the armed update fails `detected dubious ownership in repository at '/srv/kontroll.git'` | uid 1001's git did not read `<storage root>/content-clone.gitconfig` (since git's May-2024 security releases — 2.45.1, and 2.39.4 and the other maintenance lines — git refuses to serve a repository another uid owns unless it is named safe) | the same deploy writes that file before it runs git: check it is there, root's, mode `0644`, and that uid 1001 can reach it. A `--check` before a deploy has written the file (on a node none has yet, or the first after an upgrade): unarmed, root's git is given the canonical's owner as `SUDO_UID` and compares as the apply will; armed, the service's git cannot be, and compares nothing — a clone not made yet shows `would make …`, one that is there shows `ok`, with `main not compared` in the task's output (`-v`) |
| the armed update or its preview fails `cannot read refs/heads/main from file:///srv/kontroll.git` | uid 1001 cannot read the canonical | `local-canonical.yml` gives group 1001 its read and write grant: run the deploy that runs it, then this one |
| the unarmed update fails `Local modifications exist in the destination: <storage root>/api/repo (force=no).` | a tracked file of the root-owned clone was changed — only root can have done it | look at it (`sudo git -C <storage root>/api/repo status` — the clone is root's), then `sudo rm -rf <storage root>/api/repo` and deploy again |
| the armed update says `a change made in the clone and not proposed is discarded` | the service changed a tracked file in its clone and did not propose it; the update puts the clone back at the canonical's `main` | propose the change: a proposal is pushed to the canonical, not kept in the clone |

## 10. A landing was refused, or did not happen

This section also holds the refusal of an operator with the network service's uid (the row quoting `has uid 1001`):
install-prereqs.sh, the launchers and `kontroll-init.py --fresh` say it before any landing, and the deploy's first task
before its own.

Every way the operator's or the deploy's commit reaches the canonical's `main` is a LANDING
(`scripts/kontroll-land.py`; `gitio.land_commit` — SECURITY.md C10): `scripts/update.sh`,
`scripts/update-from-bundle.sh`, an onboard or a capture-exception from the CLI (`scripts/galaxy.py … --commit`),
the deploy's `local-canonical.yml`, and the GUI run from a checkout. A landing reads the canonical's `main` once,
refuses to discard what it holds, judges the worktree's commit with the promote gate, brings it across as a pack
and moves `main` in one compare-and-swap. It prints one `kontroll_run_id=… land …` line first. Exit 0 landed (or
`up to date`); exit 2 REFUSED by the gate, or the gate could not run; exit 1 nothing to land as asked. After any
exit but 0 the worktree keeps its commit and the canonical is untouched.

A promote moves the canonical's `main` past the worktree, and a landing from it is then refused until the worktree
is brought FORWARD: `python3 -B scripts/kontroll-land.py --sync` (`gitio.sync_worktree`) — what the promote put on
`main` comes across as a pack on a pipe (no fetch: nothing runs the canonical's serving side), and the worktree's
own commits since are replayed on top (with new ids; an ignored file of yours at a path `main` tracks, under one, or
where `main` has a directory, is refused before the replay and named, since the replay would overwrite it, or remove
it with its directory, without a word). The deploy runs it before it lands, as the operator, and a run that brought
the worktree forward STOPS in the sync's own task (`kontroll-land.py --sync --stop-as local-canonical`, exit 4),
naming what came in: the next run — its dry-run first — lands and deploys that tree. Under the deploy, each `--sync`
refusal below reaches you on standard output as `local-canonical: REFUSING to land — the sync of the worktree to the
canonical's main exited 1: <the sentence>`, without the `kontroll-land: ` the rows quote. The sync leaves a mark in
the worktree's git directory — before its replay, so a sync killed in the replay leaves it too — and the stop after
a sync keys on the mark: after a sync by hand, which prints what came in itself, the next deploy stops on it too —
but that deploy's playbooks are the tree that came in already, so read what the hand sync printed before you run it
(SECURITY.md C10). `update.sh` and `update-from-bundle.sh` refuse a caller that does not own the checkout's git
directory, then ask `--sync --check` and refuse (exit 3, or 1) before they fetch or merge anything. It prints
`kontroll_run_id=… sync …` first. Exit 0 in step or brought forward; exit 3 (`--check`) there is something to bring
forward; exit 1 not brought forward — HEAD, the branch and the tracked files are as they were (of the files of
`main` that git wrote before it stopped, those holding main's bytes are removed and the rest left untracked; the
sentence names both), unless the sentence says the worktree could not be put back, that git could not say whether
the tracked files are HEAD's, or that HEAD does not descend from `main` (it then names HEAD); when tracked files are
flagged skip-worktree or assume-unchanged (git's status does not compare them), the sentence says only that HEAD is as
it was, counts them (saying when `core.ignoreStat`, which flags every file, is set) and says how to name them, to
compare each with main's as git's checkout writes it (`cat-file --filters`) and, where one holds main's bytes or is
missing, to put HEAD's back. Every `git status` a sentence prints is case for case (`-c core.ignoreCase=false`), so a
file main brought beside a tracked one whose name differs only in case is named; paste each command as printed. An exit
1 that did not put the worktree back
whole — HEAD elsewhere, tracked files that differ or that git could not compare, a file of main's the replay left
untracked — keeps the sync's mark, so the next deploy stops on it once (the `STOPPED before …` row below). No hook or
fsmonitor
program runs in any git of the sync; a content filter or merge driver of your own git config does, in the replay and
in its undo (`rebase --abort`, `status`, `reset --hard`) — so configure none that names a path in the tree, and
include no config file from the tree (SECURITY.md C10).

| What you read | Where | What it means | What to do |
|---|---|---|---|
| `REFUSED: the tree does not resolve, so the next bootstrap … would stop: …` and the generator's sentence (`conflict`, `signature_policy`, `enabled_modules entry N`, `the tree holds a symbolic link …`) | `kontroll-land.py` (exit 2); `update.sh` / `update-from-bundle.sh` (`!! The landing did not happen (exit 2)`); the deploy's `local-canonical: REFUSING to land (exit 2) — …`; the CLI's `! the landing on the local canonical did not happen (rc=2)` | the commit's tree is one the promote gate refuses — the same rules as §6 and §7, judged at the door now: before this build such a tree was mirrored and then refused every promote | correct it in the checkout (the sentence names what: a fleet line, two units pinning one collection differently, a fleet-wide `signature_policy`, a link), commit, and run the same command again. The code of an update is merged in the checkout already; only the canonical lags. `python3 scripts/kontroll-land.py --skip-conflict-check` lands WITHOUT the gate — the human's override, for a node that must move while the gate cannot run, or a tree you have read yourself |
| `REFUSED: the gate could not run, so the tree was not judged: …` | the same | this checkout has no `scripts/gen-requirements.py` or `scripts/kontroll-promote.py`, the generator could not start or did not finish, or the worktree's repository could not be read | as the sentence says; `--skip-conflict-check` is the override, as above |
| `kontroll-land: the canonical (…) has commit(s) your worktree (…) does not, so this landing would discard them …` | the same, exit 1 | a promote advanced the canonical's `main` with a commit this worktree does not hold, or the worktree is on a commit that is not a descendant of `main` | `python3 -B scripts/kontroll-land.py --sync` from the checkout (the deploy runs it itself) and read what it says came in — the next deploy runs that tree as root — then run again. Never `--force` anything: the canonical is where promotes land |
| `kontroll-land: refs/heads/main moved while this landing was being judged …` | exit 1 | another landing or a promote moved `main` between this landing's read and its transaction; git refused the compare-and-swap | run it again |
| ``kontroll-land: the worktree … is a shallow clone (made by `git clone --depth …`), and …`` | exit 1, with `--check` too, before the gate and before anything is packed (the deploy's dry-run says it once the canonical exists and the sync's check before it passes; where the canonical's `main` is not in the clone, that check says the worktree is a shallow clone, and the real run's sync refuses with this sentence; on a fresh node the dry-run skips the landing's judgement and says so, and the real run refuses) | the checkout was cloned, or fetched, with `--depth`, and the rest of the sentence says what was found: HEAD needs objects the clone does not hold and the canonical does not either (on a first deploy, all the clone cut off), with git's own line; the canonical's `main` is not in the clone, or the clone stops before the history that would show it under HEAD (the sync's: that would show whether it and HEAD share history); or git could not finish judging | `git -C <worktree> fetch --unshallow`, then run again (where a promote moved `main`, the landing's sentence gives the sync — `kontroll-land.py --sync` — instead). A shallow clone lands as it is where the canonical holds what it cut off: commits on top of `main`, or a merge whose cut side the canonical holds |
| `kontroll-land: the commit is not whole in the canonical after the pack was received …` | exit 1 | the pack did not carry every object the commit names (a worktree that is itself missing objects — a damaged clone; a shallow one is refused before the pack, the row above) | `git -C <worktree> fsck`; land from a whole clone |
| ``kontroll-land: the worktree … has no `local` remote naming a canonical on this machine …`` | exit 1 | the checkout has no `local` remote, or it names something that is not a directory here (an `ssh://` URL) | `ansible-playbook ansible/playbooks/local-canonical.yml` sets it; or `--repo /srv/kontroll.git` |
| `kontroll-land: refs/heads/proposed/<id> exists already: a proposal is created once` | `--propose`, exit 1 | that run id is taken | mint another id |
| `!! REFUSING to update — the local canonical's main has moved past this checkout (a promote), or` (the first of its lines) | `update.sh` / `update-from-bundle.sh`, exit 3 (or 1, with `--sync --check`'s sentence above it) | a promote put commits on the canonical's `main` that the checkout does not hold; merging an update on top and landing would be refused at the end, with the merge already made | `python3 -B scripts/kontroll-land.py --sync`, read what it says came in, then run the update again. Nothing was fetched or merged |
| `!! REFUSING to update — this runs as uid …, and the checkout's git directory is uid …'s.` | `update.sh` / `update-from-bundle.sh`, exit 1 | the script was run by another uid than the checkout's owner (root, under sudo); what its sync, fetch and merge wrote would be that uid's | run it as the checkout's owner, not under sudo. Nothing was fetched or merged |
| `kontroll-land: HEAD's commits do not replay onto the canonical's main (…) without a conflict, in <files> …` | `--sync`, exit 1; the deploy's sync task, `local-canonical: REFUSING to land — the sync of the worktree to the canonical's main exited 1: …` | a promote and a commit of the worktree's own changed the same lines | the replay was undone and `refs/remotes/local/main` names the canonical's `main`. Read what it brings first — `git -C <worktree> log --stat HEAD..local/main`: a rebase by hand leaves no mark, so no deploy stops on it — then `git -C <worktree> rebase local/main`, resolve the named files, `git add` them, `git rebase --continue`, and run again. When the sentence adds that git's status does not compare some tracked files, check those first, as the could-not-replay row below says |
| `kontroll-land: HEAD's commits since the canonical's main (…) include a merge, which a replay would flatten …` | the same | the worktree's own history since `main` has a merge commit; the sync refuses to rewrite it into a line | read what it brings (`git -C <worktree> log --stat HEAD..<the id it names>`; a merge by hand leaves no mark), then `git -C <worktree> merge <the id it names>` and run again (the landing then fast-forwards) |
| `kontroll-land: tracked files in the worktree … have changes that are not committed …` | `--sync`, exit 1 | a tracked file is changed and not committed (untracked files do not count) | commit it, or put it aside (`git stash`), then run again. The deploy commits the instance state before it syncs |
| `kontroll-land: HEAD's commits since the canonical's main (…) include N already on another remote …` | `--sync`, exit 1 | a commit of the worktree's own since `main` is on another remote too (a remote-tracking ref other than `local`'s holds it — you pushed it, or pulled it in); a replay would give it a new id and part the branch from its upstream | as the merge row above: read what it brings, then `git -C <worktree> merge <the id it names>` and run again |
| `kontroll-land: a rebase, merge, cherry-pick, revert or bisect is in progress in the worktree …` | `--sync`, exit 1 | the worktree is in the middle of one of yours, or of a sync's replay a killed run left | finish it or abort it (`git rebase --abort`, `git merge --abort`, `git bisect reset`, …), then run again. When a rebase is in progress the sentence adds what a sync's replay cut short needs: `git -C <worktree> rebase --abort` puts HEAD back where it was before the replay (if git says `<git dir>/index.lock` exists and no git is running, remove that file first — a run killed in the replay leaves it; move aside any untracked file the abort names); then `git -C <worktree> -c core.ignoreCase=false status --untracked-files=all --ignored` (case for case) names, among your own, any file of main's the replay left: remove those before running again. `git -C <worktree> -c core.quotePath=true ls-files -v \| grep -v '^H '` (this and the commands after it are bash's: `$'…'` and `<(…)`) names any tracked file that status does not compare (skip-worktree, assume-unchanged; under `core.ignoreStat`, every file; a name it prints in double quotes is quoted the C way: give it to the shell as `$'…'`, with what is between the quotes, each `'` in it written `\'`): compare each with main's (`git -C <worktree> cat-file --filters refs/remotes/local/main:<path> \| cmp - <worktree>/<path>`; for a link, whose target `cmp` would read, its text: `git -C <worktree> cat-file -p refs/remotes/local/main:<path> \| cmp - <(readlink -nv -- <worktree>/<path>)`) and, where one holds main's bytes or is missing, put HEAD's back (`git -C <worktree> checkout --ignore-skip-worktree-bits HEAD -- <path>`). `git rebase --continue` can drop the commit it was replaying. The worktree and the lock are quoted for the shell, so the command runs as printed |
| `kontroll-land: git could not replay HEAD's commits onto the canonical's main (…): <git's reason>; the replay was undone and HEAD and the tracked files are as they were.` (and, when git wrote some of main's files first, `The files of the canonical's main that git wrote before it stopped were removed (main holds them): …` or `… are left, untracked: … (a link, bytes that are not main's, or a file that could not be removed)`, or ``Git could not say which untracked files the replay left: `git -C <worktree> -c core.ignoreCase=false status --untracked-files=all --ignored` lists them …``; and, last, `Git's status does not compare N tracked file(s) flagged skip-worktree or assume-unchanged, so it cannot say those are HEAD's: …` or `Git could not list the tracked files its status does not compare …`, the sentence then saying `HEAD is as it was` alone) | `--sync`, exit 1 | git stopped the replay for a reason that is not a conflict: commonly a file or directory in the working tree the operator cannot write (root's — a `sudo` command, or an older deploy, made it), or an untracked file of the operator's at a path `main` brings (`… would be overwritten by checkout:`, and the path). The files of main's that git wrote with main's bytes were removed: main holds them, and the next sync writes them again. No hook or fsmonitor program runs in any git of the sync (`core.hooksPath=/dev/null`) | do what git's reason says (for a path of root's: `sudo chown -R <uid>:<gid> <that path>`; for an untracked file of yours in the way: move it aside, or commit it); remove the files the sentence says are left — look at them first — since the next sync, which writes them again, is refused while they are there. For flagged files, `git -C <worktree> -c core.quotePath=true ls-files -v \| grep -v '^H '` (this and the commands after it are bash's: `$'…'` and `<(…)`) names them (`S` skip-worktree, a lower-case tag assume-unchanged; under `core.ignoreStat`, every file git wrote, and the sentence says so; a name it prints in double quotes is quoted the C way: give it to the shell as `$'…'`, with what is between the quotes, each `'` in it written `\'`): neither `git status` nor `git diff` compares one, so compare each with main's as git's checkout writes it (`git -C <worktree> cat-file --filters refs/remotes/local/main:<path> \| cmp - <worktree>/<path>`; for a link, whose target `cmp` would read, its text: `git -C <worktree> cat-file -p refs/remotes/local/main:<path> \| cmp - <(readlink -nv -- <worktree>/<path>)`) — a checkout stopped part-way can leave main's bytes in one — and, where one holds main's bytes or is missing, put HEAD's back (`git -C <worktree> checkout --ignore-skip-worktree-bits HEAD -- <path>`: a plain checkout or restore refuses a skip-worktree path as one git does not know, and `reset --hard` leaves it). Then run again. When the sentence names files left or flagged, the sync kept its mark, and the next deploy stops on it once (the `STOPPED before …` row) |
| ``kontroll-land: the replay onto the canonical's main (…) stopped and was undone — HEAD is … as it was — but git could not say whether the tracked files are HEAD's (`git status` failed: …)`` | `--sync`, exit 1 | the replay was undone, but the undo's `git status` failed (a damaged index: `index file corrupt`, `index file smaller than expected`): whether git's checkout left tracked files changed is not known | as the sentence says: if `git -C <worktree> status` fails too, `rm <git dir>/index && git -C <worktree> reset` rebuilds the index from HEAD and leaves the files alone (a `reset --hard` fails on that index too); then `git -C <worktree> status`, keep what you need, `git -C <worktree> reset --hard <the HEAD it names>`, and run again |
| `kontroll-land: git could not list the untracked files of the worktree …; HEAD was not changed` | `--sync`, exit 1 | before the replay the sync lists the untracked files, to tell what a replay that stops leaves from what was there; git could not list them — git was killed, or the index broke, between the sync's status and the listing | run `git -C <worktree> ls-files --others`: it says why; fix that, then run again |
| `kontroll-land: git could not list the ignored files of the worktree …, or the files of the canonical's main (…); HEAD was not changed` | `--sync`, exit 1 | before the replay the sync lists your ignored files and main's, to refuse an ignored file in main's way; git could not list them | as the row above (`git -C <worktree> ls-files --others --ignored --exclude-standard` lists the first, `git -C <worktree> ls-tree -r --name-only <main's id>` the second), then run again |
| `kontroll-land: <worktree> is not the top of its repository (…): name the top with --worktree; nothing was changed` | `--sync`, exit 1 | the worktree named (`.` when none is: the directory it was run from) is a directory below the top of its checkout, or one whose top git cannot say (the bracket then says so); git names the ignored files there from that directory and main's from the top, so an ignored file in main's way would not be matched | run it from the top of the checkout, or name the top with `--worktree` (the deploy does) |
| `kontroll-land: the canonical's main (…) tracks a path where the worktree … holds an ignored file, which no commit holds: <paths>. The replay would overwrite it, or remove it with its directory, without a word …` | `--sync`, exit 1 | an ignored file of yours (a rendered `.env`, a build output) is at a path `main` now tracks, or under one, or a directory of main's is where your ignored file is | move each named path aside (look at it first: no commit holds it), then run again; put back what you still need once the worktree is brought forward. HEAD was not changed |
| `kontroll-land: the replay onto the canonical's main (…) did not finish in … s, and the worktree could not be put back (…): …` (or `… stopped, and the worktree could not be put back (…): …`), ending `HEAD is … as it was, but the replay was not undone` or `HEAD is … (it was …)`, then `If <git dir>/index.lock is there and no git is running, remove it …` | `--sync`, exit 1 | the replay did not finish (it was cut off) or stopped, and its undo (`git rebase --abort`) failed — git's reason is in the brackets, often the lock file a git stopped part-way left — or did not put HEAD back | as the sentence says: if `<git dir>/index.lock` is there and no git is running, remove it; then `git -C <worktree> rebase --abort`, check that HEAD is the commit the sentence says it was (`git -C <worktree> status`), and run again. Do not `reset --hard` to the HEAD it is now: that is where the failed undo left it |
| `kontroll-land: the replay onto the canonical's main (…) stopped, and the worktree could not be put back: HEAD is … as it was, but tracked files differ from it …` | `--sync`, exit 1 | HEAD is back, but `git reset --hard` did not put back the tracked files git's checkout changed | check `git -C <worktree> status`, keep what you need, `git -C <worktree> reset --hard <the HEAD it names>`, then run again |
| `kontroll-land: the worktree refused the canonical's pack (an object in it is not what its name says, the pack is cut short, or it is larger than … bytes)` | `--sync`, exit 1 | the canonical's `main` holds an object git's strict reading refuses (a tree with a `.git` entry), or more than the bound — neither is what a promote's gate lets through — or the pack was cut short | do not bring that `main` in: look at it first, as yourself — `python3 -B scripts/kontroll-canonical.py --audit /srv/kontroll.git` (it names what in the canonical this play did not put there), then `git --no-pager -c core.hooksPath=/dev/null -c log.showSignature=false -C /srv/kontroll.git log -3 --stat --no-textconv --no-ext-diff main` (no pager, hook or program of the canonical's config runs) — something other than a promote moved it (SECURITY.md C10) |
| `kontroll-land: the mark that stops the next deploy first (…) could not be written (…); HEAD was not changed` | `--sync`, exit 1; the deploy's sync task, `local-canonical: REFUSING to land — the sync … exited 1: …` | the sync writes the mark the deploy's stop keys on before its replay, and could not: the disk is full, or the mark's path is a link, a directory or a FIFO — never written through, never waited on. A mark the failed write made is removed; one there before stays. The canonical's objects may have come across and `refs/remotes/local/main` been set; HEAD, the branch and the tracked files are as they were | fix what the reason names, then run again. If the mark's path is a link, a directory or a FIFO, the next deploy stops as the could-not-read row below says until you remove it |
| `local-canonical: STOPPED after bringing the worktree forward to the canonical's main. What came in since …:` — after `kontroll_run_id=… sync …` and `kontroll-land: brought forward — HEAD is …`, in the sync task's output | the deploy's sync task (`kontroll-land.py --sync --stop-as local-canonical`, exit 4), a real run after a promote: the run's last task, and it reports failed | the sync brought the promote's commits in and replayed the worktree's own on top; nothing of that tree was landed or deployed in this run, no task ran after the sync, and the mark the sync left is removed (or the output says it was NOT, and to remove it by hand) | read what came in — the commits (`<id> <author>: <subject>`, the newest 20 and a count of the rest), every path changed and each mode change, symlink and gitlink: the next run, its dry-run too, runs that tree's own playbooks and scripts as root. If it is what was promoted, run the dry-run (`--check --diff`), then deploy again; if it is not, do not deploy — something other than a promote moved `main` (SECURITY.md C10). The worktree holds that tree already, and the next deploy would land it: how to put the canonical's `main` back is C10's open question; until it is answered, leave the box as it is and look at the canonical as the refused pack's row says |
| `bootstrap: STOPPED before landing, deploying or running the worktree's scripts — a sync brought it forward …` (or `deploy-stack: STOPPED before …`, or `local-canonical: STOPPED before …`) | the deploy (or its dry-run, which leaves the mark), the run after a sync by hand, after a run killed before it stopped — in its sync's replay too, when a rebase may still be in progress — or after a sync refused with its replay not put back whole | a sync left its mark in the worktree's git directory and no run has stopped on it since; the real run removes the mark — not beside a rebase in progress, where it stays until the abort | as the row above. This run's playbooks are the tree that came in already — the stop holds only as long as that tree keeps it — so what the hand sync printed is the listing to trust. The stop's last sentence is by where HEAD is, asked as the operator: "in step" only when HEAD holds the canonical's main (`refs/remotes/local/main`). When HEAD is the commit before the sync — its replay undone, or cut short and then aborted or quit — the stop says the sync did not finish and that the commits listed are main's, not the worktree's: `git -C <worktree> -c core.ignoreCase=false status --untracked-files=all --ignored` (case for case: with `core.ignoreCase` true, a plain status hides a file of main's whose name differs from a tracked one's only in case) names, among your own, any file of main's the replay left; remove those (the next run, its dry-run too, would load a filter plugin among them as root, and the deploy commits untracked files as instance state); a tracked file it shows modified or deleted that you did not change holds main's bytes or lacks HEAD's (a replay quit, `git rebase --quit`, rather than aborted), and `git -C <worktree> checkout HEAD -- <path>` puts HEAD's back; and `git -C <worktree> -c core.quotePath=true ls-files -v \| grep -v '^H '` (this and the commands after it are bash's: `$'…'` and `<(…)`) names the tracked files that status does not compare, to compare with main's and put HEAD's back as the could-not-replay row above says; then run the dry-run again. When HEAD is neither, or git could not say, look at `git -C <worktree> -c core.ignoreCase=false status` and the reflog first. A mark whose first line is not a commit id (a carriage return within it too; one at its end is dropped) has HEAD before the sync `not known`. The message holds a line break, so Ansible shows it as a block: paste each command as printed. When a rebase is in progress beside the mark (a replay cut short), the stop names the abort in place of all three: `git -C <worktree> rebase --abort` puts HEAD back where it was before the replay (if git says `<git dir>/index.lock` exists and no git is running, remove that file first; move aside any untracked file the abort names), then the same status names main's files the replay left, to remove, and `ls-files -v` the tracked files it does not compare; `git rebase --continue` can drop the commit it was replaying. The mark stays while the rebase is in progress, so every run stops there until the abort, and the run after it says where HEAD is. Then run the dry-run again |
| `…: STOPPED — the mark a sync of the worktree leaves could not be read (exit …)` | the deploy: bootstrap, deploy-stack or local-canonical | git could not name the worktree's git directory, the mark could not be read as the operator (`head`'s sentence: a mark of root's, mode 0600), its git directory's path holds a line break, or it is `… is not a file a sync writes (a link, or not a regular file)` — no sync writes one, and none is followed; whether a sync brought the worktree forward is not known | for a mark that is not a file a sync writes (exit 3), the message names its path: read what came in (`git -C <worktree> reflog` names the HEAD before each sync), then remove it as yourself (`rm -r -- <path>`: it may be a directory). For a mark that could not be read as you (exit 1: `head`'s sentence names the path, and the message's `rm -f --` too), the same, with `rm -f -- <path>`; if git's directory is another uid's, make it yours as the owner row below says. For a git directory whose path holds a line break (a line feed, a carriage return or any other character Python's line split splits on: exit 4), move the worktree, or its git directory, to a path without one. For any other exit, do what git's sentence says: no removal of the mark mends it. When git refuses the repository as another user's (`dubious ownership`), the worktree's top and its git directory must be the operator's: `sudo chown <uid>:<gid> <worktree>`, and `sudo chown -R <uid>:<gid>` on the git directory (the message names the ids); git's own `safe.directory` suggestion does not reach the installer's git, which reads no global config. Until then every run stops here |
| `local-canonical: REFUSING to commit or land — git has an operation in progress in <worktree> (<path>) …` (or `… git could not say whether an operation is in progress in <worktree> (<git's last line>)`, or `… HEAD is on no branch in <worktree>, and no operation is in progress …`) | the deploy, right after the stop at the play's top — before its audit, its fleet check and its plaintext guard run the worktree's scripts as root (the dry-run too) | a rebase, merge, cherry-pick, revert or bisect is in progress in the worktree — what git keeps for it is in the worktree's own git directory, a link there too: yours, or a replay a killed deploy left (or git could not name that directory, as the operator) | `git -C <worktree> status` says which and how to end it (`--continue` or `--abort`; `git bisect reset` ends a bisect), then deploy again. For a rebase the message adds that, if it is a sync's replay cut short, `git -C <worktree> rebase --abort` puts HEAD back where it was before the replay (if git says `<git dir>/index.lock` exists and no git is running, remove that file first; move aside any untracked file the abort names), that `git -C <worktree> -c core.ignoreCase=false status --untracked-files=all --ignored` (case for case) then names, among your own, any file of main's the replay left, to remove before running again, and `git -C <worktree> -c core.quotePath=true ls-files -v \| grep -v '^H '` (this and the commands after it are bash's: `$'…'` and `<(…)`) the tracked files it does not compare (a name it prints in double quotes given back as `$'…'`, each `'` in it written `\'`), to compare with main's as git's checkout writes it (a link by its text) and, where one holds main's bytes or is missing, put HEAD's back, and that `git rebase --continue` can drop the commit it was replaying; the worktree and the lock are quoted for the shell (and so is the worktree in `git -C <worktree> status`). HEAD on no branch with nothing in progress — a replay quit part-way (`git rebase --quit`) leaves it detached on main, and the state commit would land a fragment of your commit there — is refused too, and so is a HEAD that names a ref outside `refs/heads/` (a hand-made `git symbolic-ref`; `git -C <worktree> symbolic-ref HEAD` says which; `git branch` then fails, `--points-at` too, until you switch; `git -C <worktree> for-each-ref --format='%(refname:lstrip=2)' refs/heads/` lists your branches): switch back to your branch — after a replay quit part-way, `git -C <worktree> branch --points-at ORIG_HEAD` names the branch the sync started on (unless a reset, stash, merge, pull, rebase or `git am` of your own, or a commit on that branch, came since); after a switch or detach of your own, `git -C <worktree> reflog` names it (`checkout: moving from <branch>`, above its newest `rebase (start)`: one below it is older than the sync), and where the two differ the reflog's is yours; `git -C <worktree> branch` lists yours. If git says a file is in the way — untracked, or tracked with local changes — it is the cut replay's copy of your own commit's (in the bash commands below, each `'<path>'` is its name as git lists it, a `'` in it written `'\''`): compare it with your branch's (`git -C <worktree> cat-file --filters refs/heads/<branch>:'<path>' \| cmp - <worktree>/'<path>'`; for a link, its text, in bash: `git -C <worktree> cat-file -p refs/heads/<branch>:'<path>' \| cmp - <(readlink -nv -- <worktree>/'<path>') && test -L <worktree>/'<path>'`); where it is the same, remove it (untracked) or put HEAD's back (`git -C <worktree> --literal-pathspecs checkout HEAD -- '<path>'`); where it differs, move it out of the worktree, to a directory of its own (`mkdir -p ~/kontroll-moved && mv --backup=numbered -- <worktree>/'<path>' ~/kontroll-moved/`), and look; then switch (`git -C <worktree> switch --no-guess <branch>`). The next run brings your commit forward again. Nothing was committed |
| `kontroll-land: this process runs as uid … and the worktree's git directory (…) is uid …'s …` | `--sync`, exit 1 | the sync was run by another uid than the worktree's owner (root on the operator's checkout); what it wrote would be that uid's | run it as the owner; the deploy does (`setpriv`, in the installer) |
| `kontroll-land: the canonical's main (…) and HEAD (…) share no history …` | `--sync`, exit 1 | the worktree is not a checkout of this canonical | check `--repo` and the `local` remote; nothing was replayed |
| `local-canonical: REFUSING to commit or land — <worktree>/.git holds what is not the operator's (uid …), among it: …` | the deploy, before the state commit (the dry-run too) | an earlier build's deploy ran inside the installer container as root (its `remote add`, or its state commit) and left root's files in the operator's repository; the commit and the sync now run as the operator, whose git cannot write over them. In a linked worktree the directory named is the common one | once, by hand: `sudo chown -R <uid>:<gid> <the directory it names>` (the message names all three), then deploy again. Nothing in the worktree was changed |
| `remote: C10: refused refs/heads/main — refs/heads/main is never pushed, by anyone …` | a `git push` by hand into the canonical | the canonical's hook refuses a push of `main` from everyone: nothing legitimate pushes it | `python3 scripts/kontroll-land.py` from the checkout (`--check` first to see what would land) |
| `install-prereqs.sh: the operator (…) has uid 1001, the uid the network service runs as …`, `kontroll: the operator (…) has uid 1001, the uid the network service runs as …`, `kontroll-init: the operator (…) has uid 1001, the uid the network service runs as …`, or `local-canonical: the operator (…) has uid 1001, which is the uid the network service runs as …` | install-prereqs.sh, before its root check (as root, the operator is the account that ran sudo); a launcher, for every verb before it makes anything (`shell` is told and goes on); `kontroll-init.py --fresh` run by hand (the legacy host path), as its first check (the operator a launcher passed in, else read as install-prereqs.sh reads it); the deploy's first task, behind them | the operator's uid is the network service's; the canonical's grant and hook cannot tell the two apart | install and deploy as an account with another uid. After install-prereqs.sh's sentence, kontroll-init's, or a launcher's on any verb but `shell`, nothing was installed or made under this one. After `shell` or the deploy's sentence, what this account made is its own: the home's directories and, from an earlier fresh-init or bootstrap, `instance/` and the keys under `~/.config/sops/age` and `~/.ssh`; start again as the new account, or move them to it |

## 11. kontroll-init refused the break-glass choice

`kontroll-init` checks the break-glass flags before it writes anything: every refusal below but the last leaves
`instance/` and the key directory as they were, and exits non-zero. The choices and the runbook are
[break-glass.md](break-glass.md).

| What you read | What it means | What to do |
|---|---|---|
| `--fresh requires a break-glass choice` | `--fresh` (the installer's `fresh-init`) was given none of the three | add `--break-glass-recipient <age1…>` (preferred), `--mint-break-glass` or `--no-break-glass` |
| `--mint-break-glass and --no-break-glass are choices for --fresh` | one of them was passed to an existing instance | on an existing instance, make the key on another machine and run [break-glass.md](break-glass.md) |
| `--break-glass-recipient was given a PRIVATE key` | the value starts `AGE-SECRET-KEY` (in any case, after any spaces); it is not echoed back | treat that key as exposed: it was typed on the node a break-glass key must never reach, and sat on the run's command line. Discard it, make a new one on the other machine and pass its `age1…` value (after `Public key:`); clear the old one from your shell history (`history -d`, or the history file) |
| `--break-glass-recipient is not an age public key` | the value is not `age1` followed by lower-case letters and digits | pass the `age1…` value from the `Public key:` line `age-keygen` printed, whole, without the `Public key:` prefix |
| `--break-glass-recipient is a plugin or post-quantum age recipient` | the value is `age1<name>1…` with a valid checksum, the name lower-case letters, digits, `+`, `-`, `.` or `_` (a hardware token's, `age1yubikey1…`, or a post-quantum `age1pq1…`): sops encrypts to a plugin recipient only where `age-plugin-<name>` is installed, and kontroll's images ship none; a post-quantum one needs a sops whose age knows it on every machine that encrypts or decrypts, the trusted machine's included | make an X25519 key with `age-keygen` on the other machine and pass its `age1…` value; keep the token for keys outside kontroll |
| `--break-glass-recipient is not a valid age public key (its checksum, length or padding does not match …)` | the value has an age key's shape, but a character was mistyped, added or lost in the copy (an `l` read as `1` lands here too), or its padding bits are not zero, as no `age-keygen` prints one; sops would refuse it at the first encrypt | copy the `age1…` value again, whole, from the `# public key:` line of the file `age-keygen` wrote, rather than retyping it |
| `--break-glass-recipient is a low-order X25519 point` | the value is a valid encoding of a point no key pair has as its public half (the all-zero key, say); age refuses to encrypt to it | pass the `age1…` value `age-keygen` printed for the key you made; a value built by hand is not one |
| `there is no key file at …` or `… is a key file no installer verb reads, and a run that writes the instance would key the instance to it.`, then `In the installer container (…), kontroll-init refuses a control key file anywhere but keys.txt in /root/.config/sops/age` | a run in the installer (`fresh-init`, or `kontroll-init` in its shell, under `sudo` or `env -i` too: kontroll-init also knows the image by its dispatcher, which every installer image carries, and by a marker file in images built from this release on; the parenthesis names each it found, in this order: `KONTROLL_IN_CONTAINER is set`, `<file> is there`) would key the instance to a key file that is not `keys.txt` in the directory of keys the installer mounts, there or not: most often a `--key-file` as this machine spells it (`~/.config/sops/age/<name>` typed on the host expands to this machine's home, which the container does not have); or another name, in that directory or elsewhere, which no installer verb reads; or no `--key-file` under a `HOME` other than `/root`. A key made outside that directory would be lost when the container exits, or left in the checkout or another mount, after the instance had been written to it; a key file of another name would leave the next step minting a `keys.txt` that cannot open the instance. Only an existing instance's `--break-glass-recipient`, which reads the file and writes no key, takes a key file that is not `keys.txt` in that directory, and only one that is there. Any run takes `keys.txt` itself under another path or name (a link, a hard link, a second bind), once it is there, as `keys.txt`; a directory, a FIFO or a link to no file at the path is no key file. On a host, a file at either marker path (`/etc/kontroll/installer-image`, `/usr/local/bin/kontroll-installer`) or the variable makes kontroll-init take the host for the installer container. Nothing was written | give no `--key-file`, with `HOME=/root` as the installer sets it (for a `--fresh` run, through `fresh-init`, which hands the key back): the key is then this machine's `~/.config/sops/age/keys.txt`, the one file every installer verb reads. A control key kept under another name goes there first. On a host that is not the installer, undo what the parenthesis names: unset `KONTROLL_IN_CONTAINER` if it names it, and move away each file it names |
| `no control key at …, and without --fresh kontroll-init never mints one` | the command ran where the control key is not mounted (on the host floor, outside the installer shell) | run it in `scripts/kontroll-installer.sh shell`, or name the key with `--key-file` |
| ``kontroll-init could not read a `# public key:` line from …`` | a `--break-glass-recipient` run, or a `--fresh` run where something is at the key file's path (a link to nothing counts), found no `# public key:` line it could read there: the file holds the `AGE-SECRET-KEY-` line without the comment `age-keygen` writes above it, or it is empty, not a regular file (a directory, a FIFO, a link to nothing), unreadable or not UTF-8. Nothing was written (a `--fresh` run used to scaffold instance/ first) | if it is the control key, `age-keygen -y <file>` prints its public half: add it to the file as `# public key: age1…`. Otherwise name the right file with `--key-file` (for a `--fresh` run in the installer, that file is `keys.txt` in `/root/.config/sops/age`: move the right key there), or move the stray one away before `--fresh` |
| ``the `# public key:` line in … is not the public half of the key age reads from that file`` | where an `age-keygen` program is on kontroll-init's `PATH` (an alias, a shell function or a `PATH` entry written `~/bin` does not count; the installer has one), kontroll-init asks it which key the file holds, and its answer is another key, or none: a byte-order mark before the key, a file holding only a public line, a `# public key:` line copied from another key. The line names a key the file cannot open: a `--fresh` instance would have been written to it, and on an existing instance the control-key check would have rested on the line alone. An `age-keygen` that cannot run (one on `PATH` without an execute bit included), fails, runs past 60 s, or prints what is not UTF-8 lands here too: kontroll-init cannot confirm the line. Nothing was written | if `age-keygen` itself does not run, or gives no answer within 60 s, fix that tool first, or run the step through the installer, whose `age-keygen` runs (`scripts/kontroll-installer.sh fresh-init …` for a `--fresh` run, `scripts/kontroll-installer.sh shell` otherwise), with the key at `~/.config/sops/age/keys.txt` and no `--key-file` (the installer mounts `~/.config/sops/age` at `/root/.config/sops/age`, so a `--key-file` as this machine spells it names nothing there, and every installer verb reads that one file); removing it fixes this only where another `age-keygen` on `PATH` runs: with none the check is skipped and the line is taken as written. If `age-keygen` runs, `age-keygen -y <file>` prints the public half age reads: if it fails, the file holds no key age can read (save it again without the byte-order mark, or restore it from the off-box copy); if it prints another key, correct the `# public key:` line or name the right file with `--key-file` (for a `--fresh` run in the installer, that file is `keys.txt` in `/root/.config/sops/age`: move the right key there) |
| `the key in … is not a recipient of every rule in instance/.sops.yaml` | the key file kontroll-init read (the first `# public key:` line of a keys.txt, or the file `--key-file` names) holds a key that is not this instance's control key; step 2 would have added it to every domain | name the control key's file with `--key-file`, or run it where the control key is. Do not add a key you cannot place |
| `the break-glass recipient is this node's control key` | the value is the control key's own public half | make a second key on another machine; a copy of the control key is not a second recipient |
| `--mint-break-glass shows a private key once, and standard output is not a terminal` | the run's output goes to a pipe, a file or a captured log | run it on a terminal, or make the key elsewhere and pass `--break-glass-recipient` |
| `could not mint the break-glass key (is age-keygen installed?)` | the mint failed before anything was written, most likely on the legacy host toolchain without `age-keygen` | run it through `fresh-init` (the image has age), or make the key elsewhere and pass `--break-glass-recipient` |
| `the break-glass key role (key-roles/break-glass.yml) is missing or names no recipient groups` | the shipped role file is gone or emptied, so kontroll-init cannot tell which anchors take the key | restore `key-roles/break-glass.yml` from the release |
| `could not add the break-glass recipient to .sops.yaml: …` | `.sops.yaml` names the control key but not the break-glass key. On a fresh node no secret was written yet; on an existing instance the files are as they were | correct what the sentence names, then add the key with [break-glass.md](break-glass.md) (re-running `fresh-init` is refused once `.sops.yaml` names a key) |

## 12. An answer that says `internal error (ref <id>)`

The GUI answers an exception no route designed with **500**, `code: internal_error`, and the sentence
`<what> failed — internal error (ref <id>); the service log has the details`. The id is the way in; the body never
holds the exception's text (SECURITY.md **C23**):

| Where | How to read it |
|---|---|
| the GUI's service log | `docker logs onboard-gui 2>&1 \| sed -n '/kontroll_run_id=<id>/,/^[^ ]/p'` — the line names the route, `what` and the exception's type; the frames follow it on indented lines of their own, which do not carry the id, and the range stops at the next line that is not indented (a parse error's frames run long). Never the exception's message |
| Loki | `{source="internal", run_id="<id>"}` finds that line (Vector labels its `kontroll_run_id`); the frames carry no id, so no `run_id` label: read them with `{source="internal", service="onboard-gui"}` at the line's time |
| the GUI audit log | the `internal-error` row with `run_id=<id>`, the route and the type: `{service="gui-audit"} \|= "run_id=<id>"` in Loki, or the file in `KONTROLL_GUI_AUDIT_DIR` |

A Homepage save whose overlay write fails answers 500 `write_failed` with a `ref <id>` the same way: the service
log's `homepage-write-failed kontroll_run_id=<id>` line follows the service's own `homepage write failed:
file=<name> type=<type> errno=<n>` line, and the audit row is `homepage-result error=write_failed run_id=<id>`.

No audit row where one should be? The GUI's service log says why, never with the row's detail: `audit-log write
failed: <type> errno=<n> action=<action>` for each row it could not write (errno 28 is a full disk), and `audit-log
open failed: <type> errno=<n>; no audit row is written until the GUI restarts` once when it could not open
the file at all (the audit directory, `KONTROLL_GUI_AUDIT_DIR`, missing or not writable by the GUI). Fix the
directory, then restart the GUI.

## 13. The deploy refused the storage root, or a store, inside the installer

The installer (`scripts/kontroll.sh`, `scripts/kontroll-installer.sh`) binds one storage root from the host, at the
same path: the one `KONTROLL_STORAGE_ROOT` names in the launcher's shell, `/var/lib/kontroll` when it is unset. The
deploy takes that root when nothing else names one, and refuses, before it makes anything under it, a root or a
store the installer does not bind: made in the installer's own filesystem, it would be gone with the container, and
the stack would bind Docker's `root:root 0755` directories on the host in its place (I-26). Paths are compared as
they resolve, so a `..` or a link counts where it leads. A run that passes says which root it uses and where it
came from: `storage root <root> (<source>)`.

| What you see | What it means | What to do |
|---|---|---|
| `kontroll_storage_root is <a> (an -e or an instance var), but this run is inside the installer, which binds <b> from the host …` | an `-e` or the instance's inventory vars name a root the launcher's shell does not (a shell without the `export` binds `/var/lib/kontroll`) | name one root: `export KONTROLL_STORAGE_ROOT=<a>` before the launcher, in every shell that runs it, and keep `kontroll_storage_root: <a>` in the instance vars as well, so that a shell without the export is refused, not moved back to the default root; or keep `<b>` and drop `kontroll_storage_root` |
| `<knob> is <dir>, outside the storage root <root>, and this run is inside the installer …` (with `(which leads to <path>)` after `<dir>` when a `..` or a link takes it elsewhere) | a store with its own knob (`kontroll_logs_dir`, `kontroll_backups_dir`, `kontroll_api_audit_dir`, `kontroll_gui_audit_dir`, `kontroll_ansible_log_dir`) is set outside the bound root, or leads out of it | mount that disk under the root (the message names a mount point from the knob's name; a mount, not a link) and point the knob there, or drop the knob to keep the store under the root |

## 14. No alert arrives, or the daily heartbeat stopped

Every alert goes to one webhook (`dashboards/grafana/provisioning/alerting/contactpoints.yaml`), and the heartbeat
rule always fires and is re-sent once a day (its route's `repeat_interval: 24h`), so a day without it means the
chain broke somewhere. In order:

1. **A URL is set.** A deploy without one prints `alerting: no notifier URL in the dashboards secrets domain`. Set
   it in the Secrets dialog (Dashboards → Alert notifier URL), then re-deploy grafana — the dialog shows the step
   after staging. The URL reaches Grafana only when its container is made again. A deploy that stops on
   `dashboards.alert_notifier_url must be one http(s) URL …` has a URL set by hand that `docker/.env`'s quoting or
   the log redaction cannot carry: set it again in the dialog.
2. **Grafana delivers.** Grafana → Alerting → Contact points shows `kontroll-default`'s last delivery and its
   error; Grafana's own lines are `{source="internal", service="grafana"} |= "Notify"`. A refused connection to
   `127.0.0.1:9` means the container still has the not-configured default (step 1).
3. **The rules evaluate.** Grafana → Alerting → Alert rules: `kontroll heartbeat` is Firing. Error means Grafana
   cannot query Prometheus — `docker ps` for prometheus, and the `prometheus` datasource.
4. **Grafana runs.** Nothing alerts while it is down. That is why a monitor that expects the heartbeat about every
   24h, with a grace of about 26h, is the only check that sees the whole box go quiet. With the one notifier URL,
   that is a relay of your own that forwards alerts to you and pings the monitor on `kontroll: heartbeat`; a
   healthcheck ping URL set as the notifier URL reads every alert as a success and tells you nothing.

An ntfy topic shows each alert's raw JSON as the message, unless a relay reshapes it (to be confirmed on the first
live render, `docs/logging-architecture.md` G6).

A target that never answered **this Prometheus** raises no alert (its `up` was never 1 in the 7-day window): a
rebuilt box, or a new `prometheus-data` volume, starts that window afresh, so a device down at the rebuild is silent
until it answers once. A target whose `up` (a probed device's `probe_success`) reads 1 at any of the rule's 1-minute
evaluations within 10 minutes never fires, a flapping one included; a lone answer that the next failed scrape or
probe hides before an evaluation does not count.
One dead for longer than the window stops: Grafana sends it as RESOLVED although it is still down (the window
passed, not the target; the alert's `description` says so). All by design (`metrics-rules.yaml`'s header). A
policy made in the Grafana UI is replaced at the next Grafana start; add a route to `policies.yaml` instead.

To stop alerts (a leaked ntfy topic, say), delete `alert_notifier_url` in the editor
`sops instance/secrets/dashboards.sops.yml` opens, then re-deploy grafana: the dialog keeps a blank value as the URL
already set. Grafana's database (`grafana-data`) keeps the old URL in its configuration history (SECURITY.md
R-ALERT-1).

## 15. A backup missed a device, or a scheduled job failed

`Config backup missed a device` names the device, its class and the reason, with the run's id and the Semaphore task
that ran it; `Scheduled job failed` names a Semaphore task. A backup run that fails sends both: one alert for each
device it missed, and one for its task. Each resolves about an hour after its run.

- **failed** — the capture failed: the device took the connection and its read was refused or failed (an answer with
  nothing in it, an API answer that is not 200, a session, credential or command the device refused; an SSH key or
  password it refused is one: Ansible calls that host unreachable, so its port is probed, and it took the
  connection), or the class's backup failed before its read. A device reached through a jump host is failed too, as
  no probe reaches it, and so is a device that left the run before its summary (`FAILED: it left the run before its
  summary`; its failed task is in the run's output above). The run failed. Its summary line (`<device> (<class>):
  FAILED: …`) says which; the last good capture is kept.
- **unreachable** — the device took no TCP connection at its port (the SSH one, or the API one): probed once after the
  connection or the read failed. The run passed, as one device down never fails the fleet's run. Check the device is
  up, and its address and port. The probe reads the host's `ansible_port` (else 22) or the API recipe's port: where
  the port is set only in an SSH config, the probe is of another port, and a refused key can read as unreachable.
- **not-run** — the run recorded nothing for the device (`not run — no result: its backup recorded none`): its class's
  backup records no result, which is the class's fault, not the device's.

A device the run skipped (`skipped — …`: not backup-capable, or its class's backup config missing) has its summary
line and no miss, so no alert names it.

A run outside Semaphore names its task `none`, and a dry run names no device. The run's stamp and its misses:
`{source="internal", service="ansible", run_id="<run>"}` (only those lines name the run); the run's own lines are
`{source="internal", service="ansible"}` from its stamp's time; its misses alone:
`{source="internal", service="ansible"} |= "KONTROLL-BACKUP-MISSED"`. A failed task's output is in Semaphore (Task
History, the task the alert names); Semaphore's own line is
`{source="internal"} |= "Failed to run task" | json container_name | container_name = "semaphore"`. A job that
selects no host (a `--limit` naming a class with no host in the inventory) fails so too.

No alert for a run you know failed: the run log reaches Loki only where the job writes it. A box deployed before I-82
needs semaphore re-deployed (`docker exec semaphore printenv SEMAPHORE_FORWARDED_ENV_VARS` names `ANSIBLE_LOG_PATH`);
a template whose Environment sets `ANSIBLE_LOG_PATH` writes its job's log there instead, which Vector does not read;
then §14. A job stopped by hand, or one Semaphore could not start (before it ran the playbook), logs no `Failed to run
task`, so the job alert says nothing of it.

## See also
- [../SECURITY.md](../SECURITY.md) **C23** — an error body carries no exception text; the id that finds the details
- [break-glass.md](break-glass.md) — the break-glass key: the choice at install and the runbook for an instance without one
- [logging-architecture.md](logging-architecture.md) — the layered design + the degrade-path source of truth
- [../SECURITY.md](../SECURITY.md) **C22** — the request boundary, the refusal that is the repository's own, and a
  fleet line that is not a class key
- [SETUP.md](SETUP.md) §3 — the fleet file
- [SETUP.md](SETUP.md) §6 — the storage root and its stores (the installer's refusals: §13 above)
- [observability/logging-capability-vector.md](observability/logging-capability-vector.md) — the logging capability
- [../SECURITY.md](../SECURITY.md) **C12** — logs are a secret surface (label hygiene, mgmt-only, at-rest)
- `dashboards/grafana/dashboards/logs.json` — the same signals as a Grafana pane
