# Changelog

> Entries dated before the public release (2026-10-08) were written in the private development repository.
> `#NNN` numbers refer to its pull requests/issues, `docs/reviews/…` paths and "report/design NN" references are
> internal design records that are not published, and `local/…` paths are git-ignored operator notes. They are
> kept for provenance only.

All notable, behaviour-affecting changes. Newest under `[Unreleased]`; promote
to a dated section at milestones. Convention mirrors the migration repo and the
NetConfig project.

## [Unreleased]

### docs: the design records say they are history, on their first line (2026-10-19)

Several design documents still opened with a status that was no longer true: "DESIGN ONLY — not built" above a
design that has shipped, "Still design: … a GUI" in a tree that has one. A reader arriving by a link took them for
descriptions of the tree.

- **Each design record opens with a banner:** design history, when it was written, whether it was built,
  superseded or parked, and that the code and `SECURITY.md` win where they disagree. That covers the eight
  history records under `docs/observability/` (two documents there are current references and carry none),
  `docs/observability-onboarding-flow.md`, `docs/capability-matrix.md`, `docs/api-architecture.md` and
  `docs/qa-and-release-pipeline.md`. Every path stays where it was, so nothing that cites one changes.
- **The false status clauses are gone or corrected:** the "DESIGN ONLY, not built" clauses, the master design's
  "NOTHING HERE IS BUILT", the capability matrix's "Still design" list, the API's published-OpenAPI workstream
  (built), and two items of the QA plan that exist (`CONTRIBUTING.md`, Dependabot).
- **`docs/observability/README.md`** (new) says what the directory holds and names the two current references. The
  README's table lists the design history.

### fix(deploy): a fresh node's storage root is made root's, and the refusal asks for one the service's git can pass (2026-10-16)

Nothing in `deploy-stack.yml` made the storage root itself. Ansible's `file` gives every parent it creates the
owner and mode of the path it was asked for, so on a fresh host-direct node the first store made the root its own
— the Vector dir's 10002 `0700`; without Vector and Prometheus, the backups dir's 1001 `0700`, Loki's 10001
`0750` or an audit dir's 1001 `0755`, whichever the services ask for first — and the content clone's refusal (the
2026-10-14 entry) then stopped the first apply. An installer-mode node escaped it: Docker makes a missing bind
source root's, `0755`.

- **A missing storage root is made root's, `0711`,** right after the play checks its path and before any store is
  made in it (a missing parent of a relocated root is made the same). A root that exists is not changed (a link,
  an operator's mount); one that is not root's alone, or that others may not pass through, is refused, with the
  fix. Every local account can now reach a store below the root by its name; what closes a store is its own mode
  (SECURITY.md C10).
- **The refusal also asks that anybody may pass through** the clone's parent and the storage root. Armed, the
  service's git (uid and gid 1001, no supplementary group) reaches its clone and the configuration file through
  both by the bit for others, and under a root-owned `0700` root it stopped at `could not create leading
  directories` — what the fix printed before
  (`chown root:root`, `chmod go-w`) left of a root an earlier build made 1001's `0700`. The fix it prints is now
  `sudo chown root:root <path>` and `sudo chmod 0711 <path>`. It is asked unarmed too, so arming never finds the
  root closed.
- **Tests:** `tests/unit/test_content_clone_tasks.py` (the root is made only when missing, root's `0711`, before
  every task that writes under it; the refusal reads `xoth`); `tests/integration/test_content_clone_as_service.py`
  runs the play's own tasks, in its order, on a node with no storage root (the root ends root's `0711`, the Vector
  dir keeps its owner, the clone is made by the uid the flag names), and a root closed to the service stops the
  deploy before any git, after which the printed fix opens it.

**On a box that runs an earlier build:** look first — `sudo stat -L -c '%u:%g %a %F' /var/lib/kontroll` (or your
storage root; `-L` because the refusal judges what a link there points at). Anything but a directory of uid `0`
with no group or other write bit and an execute bit for others meets the refusal on the first deploy of this
build that brings up the API or onboard-gui: make it so with the two commands above, then `--check --diff`, then deploy.

### docs(process): a reader measures on a copy, in containers named for it — the runner's preamble says what a seed may allow (2026-10-15)

The runner's read-only preamble said an agent runs no git, Ansible or docker at all, while a run's seed often asks
its readers to run the suite and single-fix reversions in the local gate image. The preamble
(`.claude/workflows/blackboard.js`) now says what a seed (or a single reader's task) may allow: a measurement on
the agent's own scratch copy of the tree read — the one write besides its report — outside every worktree and
never against the lab, with every container named for the agent, mounting only that copy, and stopped or removed
by that name only — never by image or by a pattern over `docker ps`; nothing pruned. Read-only git (log, show,
diff, archive) may read a worktree, writing only to stdout or into the copy. Readers run side by side, and one
reader's filter on an image name once stopped another's mutation containers, whose exit status then read as
"caught". `CLAUDE.md`'s hard rule, `docs/agent-workflow.md` (with a "Measuring" section in the seed template) and
`.claude/workflows/README.md` say the same. The preamble's pinned digest in `tests/unit/test_review_loop.py`
changes with it, and the test now pins the measuring rule's clauses as phrases too, so that a later re-pin still
has to carry them.

### fix(deploy): the deploy's git in a content clone runs as the clone's owner — root runs none in a clone the service writes (2026-10-14)

Armed, a network service commits into its content clone (`<storage root>/api/repo`,
`<storage root>/onboard-gui/repo`), so it writes that clone's config and hooks; the deploy runs as root. The
deploy ran the git module and `git remote add` in that clone as root, with a system-wide `safe.directory` entry so
git would not refuse it: whatever the service had put in the clone — a `core.hooksPath` hook, a `core.fsmonitor`
program, a filter — ran AS ROOT at the next deploy. And every armed deploy chowned the clone recursively to uid
1001, by path, in a tree that uid writes.

- **The rule:** root runs no git in a clone the service uid can write, and changes no owner by path in a tree
  that uid writes. The tasks are one file, `ansible/playbooks/tasks/content-clone.yml`, imported for each
  surface.
- **Armed:** root makes the clone's directory, empty; git run as uid 1001 clones into it and updates it —
  `setsid --wait setpriv --reuid=1001 --regid=1001 --clear-groups --no-new-privs -- env -i …`, from `/`, its script
  not rewritten from the deploy's environment (`expand_argument_vars: false`), configured by one file root writes
  (`<storage root>/content-clone.gitconfig`, which names the canonical safe: since git's May-2024 security
  releases — 2.45.1, and 2.39.4 and the other maintenance lines — git refuses to serve a repository another uid
  owns, and `-c safe.directory` does not reach the serving side of a local clone). An existing clone fetches
  `refs/heads/main` by its full name and checks `main` out at it; a change made in the clone and not proposed is
  discarded, and the deploy says so (the git module refused to update over one); its `local` remote is put back
  at the canonical. `changed` is the clone made, `local` added or put back, `HEAD` moved or put back on `main`, or
  a change discarded; a second deploy reports none. `--check --diff` changes nothing and says what would; a
  canonical uid 1001 cannot read fails the preview as it fails the deploy.
- **Unarmed:** the clone is root's and the git module updates it as before — now with the system file switched
  off and the same configuration file, so a root deploy that is not under `sudo` (no `SUDO_UID`) can serve the
  operator's canonical on a git that checks ownership.
- **What stands at the clone's path is root's to place:** each clone's parent (`<storage root>/api`,
  `<storage root>/onboard-gui`) is now root's in both modes — its `tls/` stays the service's — and the tasks
  refuse to go on unless the parent and the storage root are directories of root's that neither their group nor
  anybody else may write.
- **A clone held by the wrong uid is made again, not chowned:** anything in it the service can write while
  unarmed (every entry is looked at); anything not the service's — root's, another uid's, a link, a file — while
  armed. It is removed (Python's rmtree, from open directories) and its owner makes it again; the service is
  restarted after `up` so its bind mount is the new directory.
- **Gone:** the two "Trust the … clone for root's git" tasks (`git config --system --add safe.directory …`),
  the recursive chown of each clone, root's `git remote add`.
- **Tests:** `tests/integration/test_content_clone_as_service.py` (in `validate.sh`'s as-root step) runs the task
  file with ansible-playbook as root, holding a terminal; programs planted in the clone write down who they run
  as — uid and gid 1001, no other group, no new privileges, no capability, no terminal, no variable of the
  deploy's. One row runs the armed task's own command from a process that holds a terminal, so the terminal is
  shown gone whatever ansible-core the gate has (2.19's workers drop it by themselves; 2.18's — the installer's —
  do not). `tests/unit/test_content_clone_tasks.py` holds the structure; `tests/check-storage-chown.py` follows
  an `import_tasks` (with its `vars:`), and `.ansible-lint` lints `ansible/playbooks/tasks/` as task files.

**On a box that runs the previous build:** the clones need nothing. The first deploy makes each clone's parent
root's. Its storage root it does not change: look at it before the first deploy (the 2026-10-16 entry). An armed
box keeps its clone (its top is the service's; the one file the previous build left root's in
it, `.git/config`, stays root's and readable), and from now on only uid 1001 runs git in it. A box deployed
unarmed after it was armed, or armed after it was unarmed, has its clone made again by its owner; the first such
deploy reports it. Dry-run first: `--check --diff`. A dry-run changes nothing, so its first run stops at a clone's
parent, armed or not: the previous build left it the service's. The `sudo chown root:root <parent>` it prints is
enough (it is what the deploy does there first, and the parent's `0755` passes); then dry-run again. Or make both
parents root's before the first dry-run: `sudo chown root:root /var/lib/kontroll/api /var/lib/kontroll/onboard-gui`,
with your storage root's paths, for each that exists (no service mounts a parent itself, so the running build is
not disturbed). On its first full run the armed preview says `main not compared` (the configuration file is
written by the deploy itself). **On a host-direct box, remove the system-scope
`safe.directory` entries the previous build added** (one per armed deploy, for each content clone): while one
stands, a git you run as root in that clone trusts it, and so runs what the service put there, as root. Remove
exactly those, with your storage root's paths —
`sudo git config --system --fixed-value --unset-all safe.directory /var/lib/kontroll/api/repo` and
`sudo git config --system --fixed-value --unset-all safe.directory /var/lib/kontroll/onboard-gui/repo` (exit
status 5: there was none). A value-pattern such as `'/(api|onboard-gui)/repo$'` would also remove an entry of
yours that ends the same way. Inside the installer container they never outlived the run.

### feat(canonical): `kontroll-canonical.py --audit` — a deploy names what is in the canonical that the play and the tool do not write (2026-10-13)

A bare repository has many places where one more file changes what git does there: a config key, a hook, an entry
of `info/` or `objects/info/`, a ref of a kind the tool does not write, a top-level entry the play does not make.
The tool's own git runs in an environment of its own — so nothing has ever said that one of them is THERE, and
what a node's canonical really holds had not been read by anything.

- **`python3 -B scripts/kontroll-canonical.py --audit <repo>`** (from the checkout) prints one line per finding —
  a kind and a key, never a value (the name of a config key and not what it is set to; a name is shown escaped
  and cut, and each kind is cut by itself, so many lines of one kind cannot push another kind out). Exit 0 with
  the report; `--strict` exits 3 when there is a finding; 2 for a usage error or a path that is not a directory;
  1 when the audit ended on an error of its own, which its last line then says.
- **It reads the repository as files, from open directories.** The directory it reads is one somebody else may
  be writing meanwhile, so each directory is opened from the one above it and never through a link, and a file
  is read only when the thing that was opened is a regular file (a pipe named `HEAD` does not hang it; a
  directory exchanged for a link while it is read is not listed). It reads `HEAD`, the ref files, `packed-refs`
  and `hooks/update` itself. It starts one process: `git config --file - --no-includes --list -z`, handed the
  config file's bytes on its standard input — bounded first, in size and in the length of a line — and never the
  repository. `--no-includes` matters: from standard input git follows an `include.path`, which it does not when
  asked of a path. Started by root for a repository the service can write, that git is the service uid
  (`canon.identity_kwargs`).
- **A key the play sets is compared, not only named.** Missing, there twice, or set to something else is a
  `config-value` line (the value itself is never printed). `hooks/update` is compared with the file the checkout
  ships, and is named when git would not run it (no execute bit) and when it, or `hooks/`, is the service's to
  write.
- **`local-canonical.yml` runs it on every deploy, BEFORE its first write into a canonical that is already
  there** — under `--check` too — and shows what it named; when the audit gave no report, a sentence says so.
  It never fails the play: whether a finding should ever stop a deploy is not decided. Because it stands in
  front of the play's own repairs, what the play is about to put right (`HEAD`, its keys, the hook) is named in
  that run; a line that returns on the next deploy is something that changes the canonical between deploys.
- **git's own housekeeping leaves nothing the audit names.** The reflog git keeps under `logs/` for `HEAD`,
  `main` and a proposal is the tool's own doing (the promote runs git in the tool's environment, where git
  keeps one); any other file under `logs/` is a finding. And the play now sets two more keys in the canonical —
  `gc.writeCommitGraph=false`, `repack.updateServerInfo=false` — so that the collection a push starts by itself
  no longer writes `info/refs` and a commit-graph there. Nothing here reads either, and one that is there is now
  somebody's.
- **The play's `git config` replaces every occurrence of a key** (`--replace-all`). A key that is in the file
  twice is one git will not set with a single value: one more line in `config` stopped every deploy at "Harden
  the bare repo", with the last line in force. Now the audit names it and the play puts it right.

**On a box that runs the previous build:** the first deploy of this build opens with the audit of the canonical
as it is. Expect, once: `config-value: gc.writecommitgraph` and `config-value: repack.updateserverinfo` (the play
had not set them), `hook-changed: update` when the installed hook is older than the shipped one, and — where git
has collected garbage there before — `info-entry: refs` and `objects-info-entry: commit-graph`: remove those two
files once. Anything else is a finding; `docs/troubleshooting.md` §8 says what each line means and what to do.

**New here:** `tests/unit/test_canonical_audit.py` — a canonical the play made is clean, and still is after the
tool used it and after git collected garbage in it (the play's keys and its command line are read from the
play); each planted thing is named by kind and key and its value is nowhere in the output; an include is named
and not followed; a name that is not plain text is one escaped line; no link is followed, no directory that is
exchanged under the reader is listed, and a pipe is not waited on (the rows that would hang fail instead); the
one process is held by a closed list of what the script may reach for; every kind the script prints has a row
in both tables; the play audits before it writes, and its conditions are evaluated; a key that is there twice is
named and then put right by the play's own command. In `tests/integration/test_canon_drop.py`: that one git is
the service uid when root audits the canonical.

### fix(canonical): a root caller runs the canonical's git as the service uid — what the canonical's files can make a git do, they make it do as the service that wrote them (2026-10-13)

When a deploy is armed the canonical's own files — its config, its refs, its packs — are written by a network
service, and `sudo kontroll-promote` is root. The hardened environment every git of the canonical runs in
(`canon.git_env`) answers each way a repository makes git run a program or read another store that somebody has
listed. This is for everything else — the parser of a pack or of a ref file, and whatever else those files can
make a git do: run by root, it was root that did it (measured).

- **`canon.git_kwargs`: for a caller with effective uid 0, in a repository of which anything is the service's to
  write, every git of the seam is started as uid:gid 1001 with no supplementary group** — in a session of its
  own, with no standard input of the caller's unless the tool feeds it (so not holding the terminal root sits
  at), and in `/` (the caller's own directory may be one the service cannot enter). The promote, the Pending
  listing, the discard and the export all start git through it. The service's containers and the Semaphore job
  are that uid already and are not touched; nor is a repository the service can write nothing of (root's own —
  the unit suite's, run as root).
- **`canon.service_writable` asks the whole repository**: the top directory, then every entry below it — its
  owner, its group's write bit, everybody's, a symbolic link, and an access-control list where the group's write
  bit shows one could grant something. Not one directory: with write on `refs/` the service can exchange
  `refs/heads` for a link to a root-owned directory, for nothing, or — where one stands beside it — for a
  directory of somebody else's with no group write bit, and a single look at it was talked out of its answer
  each way.
- **`kontroll-promote.py` — and its `--list` — refuse a caller who is neither root nor the service uid** on a
  repository the service can write: "run this with sudo". Run as themselves, git would open that store with
  their account. As root it says on stderr as whom git runs.
- **A root that may not change identity is refused in one sentence** ("could not drop to the service uid") — a
  container started without `CAP_SETUID`/`CAP_SETGID`, or in a user namespace that denies `setgroups`. Nothing
  runs as root instead.

**Covers:** the gits this tool starts through the seam. A git started any other way runs as its caller, as before.

**On a box that runs the previous build:** nothing to do. `sudo kontroll-promote` works as before and is the
supported form; it does not depend on where it is typed (git is started in `/`), and the files a promote leaves
are now the service's (group-writable, as the Semaphore job's are), not root's. A promote run WITHOUT `sudo` by an
account in the canonical's group is refused where it used to run.

**A maintainer's call:** whether members of the canonical's group are refused or only told. Built: refused.
Letting them through is the one refusal in `kontroll-promote.py`'s `main` that says so, printed as a note, and the
row `test_who_may_run_the_promote_and_as_whom_it_says_git_runs`.

**Corrected in passing:** SECURITY.md's `hooks/` paragraph; the sentence of C22's known gaps that named who runs
the promote and said it takes its run id unchecked (false since the promote reads one id); the sentence that gave
the CLI promote as a reason for the canonical's group; and the reason `scripts/lib/canonical-group.sh`, the two
launchers and `scripts/install-prereqs.sh` give for that group (the operator's own pushes, not the promote).

**New here:** `tests/integration/test_canon_drop.py` — as root, with real uids, on a canonical laid out as
`local-canonical.yml` leaves one: a hook that writes down who it runs as writes uid, gid and groups 1001 during a
promote by root (and 0 in a repository that is root's own — the control); the child holds neither root's terminal
nor its standard input; the promote works from a directory the service may not enter; the listing, the promote,
the tree read and the discard all work as the service with no `safe.directory` anywhere, and what they leave is
the service's and group-writable; the exchanges of `refs/heads`; one thing of the service's deep below a closed
top directory; the canonical reached through a link; a caller who is neither, patched and as a real third
account; a root without the capability. `tests/validate.sh` runs the file as a step of its own — as root, or
through `sudo -n`, and it says which — and a skipped row fails it (`--strict`: so does a gate that cannot be
root). In `tests/unit/test_canon.py` and `tests/unit/test_kontroll_promote.py`: the decision itself, the numbers
(1001, as the compose fragments and the play have them), and the sentences the troubleshooting table quotes, on
any host. The seam pin (`tests/unit/_readonly_pins.py`) refuses a call site that passes an identity, a session or
a descriptor keyword of its own beside the seam.

### feat(promote): `--onto <commit id>` — a judgement that depended on `main` names the commit it was made against (2026-10-13)

The promote lands the commit that was judged — but "judged" can mean two things. The gate judges the proposal's
TREE: the same whatever `main` is. A caller that reads `main` itself and judges the proposal's DIFF against it —
which paths change — has approved a change relative to THAT commit, and the promote reads `main` for itself a
moment later. A proposal that is a fast-forward of both could then land a change that, measured from where
`main` had moved to, nobody judged (it may take back what was promoted in between).

- `kontroll-promote.py … --onto <commit id>` promotes only if `refs/heads/main` is that commit (exit 1 otherwise:
  "main is not the commit this promote was to land on"). The transaction moves `main` only from the commit the
  command read, so the named commit is the old value of the one ref update.
- The operand is a full commit id, never a name.
- Nothing passes it today: the `promote-proposal` job and a person at a shell approve a commit, and the gate
  judges its tree. It is for a caller that classifies a change list.

**New here:** in `tests/unit/test_kontroll_promote.py` — a proposal that is a fast-forward of the `main` it was
judged against AND of the commit `main` moved to is refused with `--onto` (control: it lands without); a name in
the operand's place; `main` moved after the command's own read.

### fix(canonical): the canonical's `update` hook is an allow-list — the network service's uid may create a proposal by push, and nothing else (2026-10-13)

The hook on the canonical refused one thing: a push to `refs/heads/main` from the network service's uid (1001).
Everything else was allowed — so that uid could push a tag called `main` (which a short name finds before the
branch), a `refs/replace/*`, or the same `proposed/<run id>` a second time, re-pointing a proposal after a human
had read it. A deny-list of one name is a list of everything else that is allowed.

- **As uid 1001**, a push may **create** `refs/heads/proposed/<run id>` — a minted run id, either shape — and
  nothing else: not `main`, not a tag, not `refs/replace/*`, not `refs/proposed/…`, and not a move or a delete of
  a proposal that exists ("a proposal is created once; stage a new run id").
- **As any other uid** (the deploy's mirror, the installer): `refs/heads/main` and a proposal, and nothing else —
  a release tag or another branch pushed to the canonical is refused.
- A delete is refused from everyone; so is every push when the pusher's uid cannot be read.
- The hook says on stderr which ref it refused and by which rule.

What it does not stop is unchanged and is said in the hook's header: a write that is not a push runs no hook.

**On a box that runs the previous build:** the hook file is replaced at the next deploy. From then on a tag, or
any branch other than `main`, pushed to the canonical is refused; a proposal staged before is unaffected and
promotes as it is. A ref that is already in the canonical is not removed: a tag or a branch pushed there by an
earlier build stays until someone removes it on the box.

**New here:** in `tests/unit/test_staging_isolation.py` — the committed hook run over a table of pushes (both
uids, every ref kind, a uid that cannot be read), the run-id samples fed to the hook and to
`canon.valid_run_id` alike, and real pushes into a repository that carries the hook (a second push to a proposal
is refused and the proposal keeps its first commit; the promote is not a push and is not in its way).

### fix(promote): what is judged is what is promoted — the promote reads the proposal once, lands that commit in one ref transaction, and an approval names the commit (`<run id>@<commit id>`) (2026-10-13)

A promote was a sequence of steps — a preview, the resolve gate, a fast-forward check, the move of `main`, the
delete of the proposal — and each looked the proposal's NAME up for itself. A proposal is a name, and between
two steps the name could come to mean another commit: the commit previewed, the commit the gate judged and the commit that landed could be three, and the commit an operator
read about in the Pending panel was not necessarily any of them. SECURITY.md C10 named this as the gate's
remaining limit; this closes it.

- **One read, then ids.** `kontroll-promote.py` reads `refs/heads/main` and the proposal once, each by its full
  name (`canon.read_ref`), and looks no name up again: the fast-forward question, the preview and the gate are
  asked of the two commit ids. A ref of the proposal's name that is a symbolic ref, or names no commit, is not a
  proposal (exit 1).
- **One transaction.** `gitio.promote_commit` moves `main` with a single `update-ref --stdin` transaction that
  verifies the proposal is still the commit that was judged and updates `main` only from the commit it was read
  as, neither following a symbolic ref. A proposal re-pointed after it was judged is refused ("no longer the
  commit that was judged"); so is a promote that lost a race to another ("main moved"). `main` is untouched in
  both cases. The proposal is retired by compare-and-delete with `--no-deref`.
- **In the seam.** Every git of the promote runs in `canon.git_kwargs`: the canonical's hooks and config-named
  programs do not run on it, and of the promoter's environment git is handed where programs and temporary
  files are and the locale, and nothing else — no `HOME`, no `GIT_*`, no key.
- **An approval names the commit.** The promote takes `<run id>@<commit id>` and refuses when the proposal is
  not that commit ("not the commit you were shown"). The Pending panel's copy control now copies that form
  ("copy to promote"); `kontroll-promote.py --list --repo <canonical>` prints it for every staged proposal. The
  `promote-proposal` Semaphore job requires it (`--require-pin`; its survey field is now "Proposal
  run_id@commit"); at the box's shell a bare run id still works and promotes the commit the read returns.
- **The promote prints what it lands, before it lands it:** `kontroll_run_id=<run id> promote proposed/<run id>
  commit=<commit id> -> main (was <commit id>); commits=<n>` and the commits (id, subject — escaped where it is
  not text, cut where it is long, the rest counted). The listing is read as committed, one record per commit that
  names its commit twice, and opens with the commit that lands. That output — the Semaphore job log, or the
  terminal — is the record of what was approved (at a shell nothing keeps it: an approval that must be on file
  goes through the job). The play's own `git log` preview and
  existence check are gone (they ran by short name, outside the seam), and no message sends a person to run git
  in the canonical by hand.
- **A refusal says what happened.** A transaction git refuses while both refs are as they were read is said as a
  held lock (not as "`main` moved"); a git that was stopped at its time limit is reported by what `main` IS, read
  afresh; a proposal left at `main`'s own commit (a promote that moved `main` and could not delete the ref) is
  "`main`'s commit already" — and the Pending panel lists such a row as not promotable, so its Discard control
  is offered.
- **Exit codes:** 1 — not a run id or not a full commit id, no such proposal, not a proposal, not the commit
  named, not a fast-forward, the proposal is `main`'s commit already, the canonical unreadable, or a ref moved
  meanwhile; 2 — the gate refused the tree or could not run. An option-like word in the run id's place
  returns 1 instead of leaving through the argument parser.

`gitio.promote_ref(<run id>)` remains as a wrapper that reads the two refs and calls `promote_commit`.

**On a box that runs the previous build:** the `promote-proposal` job asks for `<run id>@<commit id>` — copy it
from the Pending panel ("copy to promote") or print it with `kontroll-promote.py --list`; a bare run id is refused
there. A proposal staged under the previous build is promotable as it is. After the next deploy, run
`./kontroll configure-semaphore` (`python3 scripts/configure-semaphore.py`): it updates the registered
`promote-proposal` template in place, survey text included. The deploy does not run it. Until it has run, the
job's survey still reads "Proposal run_id" and still describes a bare id — and the job refuses a bare id, with
the sentence that says what to enter.

**New here:** `tests/unit/test_promote_cas.py` rewritten against real git — the races injected between the
read and the transaction, a symbolic proposal costing only its own name, no hook on a promote, the refusal of a
canonical the seam refuses, a string that is not a run id never reaching the transaction text, a held lock, a
git that cannot start or is stopped at its time limit, the retire's argv, the wrapper. In
`tests/unit/test_kontroll_promote.py` — the commit judged is the commit landed, the pin, `--require-pin`,
`--list` (its order, what git left out, its exit status), the races through the whole command, the exit codes,
the preview (the count, the cut, the escapes; printed before the gate judges), a terminal that cannot encode a
subject, a directory named `.git` inside the canonical; the command starts one process and it is the generator.
In `tests/unit/test_canon.py` — the play read as YAML (one command, the pinned assert, no raw git),
`commits_between` (a commit with no message, an output encoding the repository names, a listing that is not in
the form asked for), `git_dir`; the seam pin follows a pinned function into the helpers it calls.
`tests/e2e/test_pending_changes.py` — each row's copy control yields that row's `<run id>@<commit id>`.

### fix(promote): the promote gate judges the proposal's commit itself — read object by object, every object re-hashed — and fails closed (2026-10-13)

Before a promote fast-forwards `main`, a gate hands the proposal's tree to the collection generator and refuses
what the generator would refuse. It read that tree as `git archive` exports it. An export is the commit as its own
`.gitattributes` make it: one `export-ignore` line took a conflicting unit, the instance overlay, or all of
`modules/` out of what was judged — and to a tree with no `modules/`, or one it could not unpack, the gate
answered "nothing to check". SECURITY.md C10 named these as limits of the gate as built; this closes them.

- **The gate reads the commit** (`canon.read_tree_verified`): one `git cat-file --batch` answers for the commit,
  each tree and each blob, and every object is **re-hashed against the id it was asked for** — git does not check
  that an object it reads is what its name says, so an object file swapped under its name was judged as if it
  were the committed one. Trees are parsed from their own bytes. No attribute, filter, `info/attributes`, replace
  ref or commit-graph has a say; no program of the repository's runs.
- **Regular files only.** A proposal whose tree holds a symbolic link, a submodule entry, a name no checkout could
  take (a separator, a control byte, `..`, a spelling of `.git` that git's own checkout refuses — `git~1`, `.git`
  with trailing dots or spaces or a `:stream` — or a name longer than a file system takes), or one name twice in a
  directory, is refused whole (exit 2). The old gate unpacked the export with `tar`, which created the link.
- **It fails closed.** A tree the reader refuses is a refusal; a proposal with no `modules/` where `main` has one
  is a refusal; and a gate that cannot run — the repository cannot be read, the checkout has no
  `scripts/gen-requirements.py`, the generator does not start or does not finish — refuses with its own sentence
  ("the gate could not run … was not judged"). "Nothing to check" is left for a repository in which neither `main`
  nor the proposal has a `modules/`. `--skip-conflict-check` is still the human's override.
- **Two things outside the proposal's commit are refused rather than believed.** A `main` that is a symbolic ref or
  names no commit refuses every promote. A proposal that deletes the fleet file where `main` has one is refused:
  the generator would have judged this checkout's shipped example in its place.
- **Whose fault a failure is.** An object file git cannot read, and a name no file system takes, are refusals of
  the tree — not "the gate could not run", the sentence that names the override. When the generator fails on a
  file of the proposal, its last lines are shown and the rest are counted.
- **The run id must be a minted one** (exit 1 otherwise): nothing else is formatted into a ref name, and a ref of
  that name that is a symbolic ref or names no commit is refused as not a proposal.
- **New `scripts/kontroll-export.py`**: `--repo <git dir> <commit id> <dest> [<prefix> …]` writes one commit's
  files into a directory by the same read — for a play or a script that today pipes `git archive` into `tar`.
  The whole export or nothing; exit 2 means the commit's tree is refused and nothing else (a usage error, a
  prefix that selects no file, a repository that cannot be read are exit 1).
- `tests/validate` holds the shipped tree to the gate's rule: no symbolic link, no submodule entry.

**Still open, and said in SECURITY.md:** the gate judges the commit the proposal's ref names when it reads it; the
fast-forward resolves the name again.

**On a box that runs the previous build:** a checkout without `scripts/gen-requirements.py` promoted before (the
gate skipped itself) and is now refused with a sentence; a proposal that carries a `.gitattributes` is judged in
full; a proposal that commits a symbolic link is refused. `docs/troubleshooting.md` §7 has each sentence.

**New here:** `tests/unit/test_canon_tree.py` — the reader against an attribute, a filter, a replace ref, a lying
commit-graph, an object swapped under its name (the blob, the tree, the commit), every entry it refuses — the
spellings of `.git` with git's own refusal as the control, and the names beside them that are still read — a
sha256 repository, the caps and their boundary, a git that cannot run, stops answering, ends in the middle of an
answer or dies on one object (whose fault each is); `write_files` and the export command (its exit codes, a
prefix that selects nothing, a move-in that fails part-way). In `tests/unit/test_kontroll_promote.py` — a
proposal that hides its conflict behind `export-ignore` is refused on it; the generator is handed the committed
bytes; a swapped object, a link and a removed registry exit 2; a `main` that is not a plain branch, and a
proposal that deletes the fleet file, are refused; each way the gate cannot run refuses and says so; a generator
that fails without a word still refuses; only a minted run id reaches git.

### fix(pending): the Pending list and the discard read the canonical by full ref name, in one hardened git environment, and as bytes — a tag called `main`, a planted hook or graft, or a name that is not text no longer changes or breaks the list (2026-10-13)

The Pending panel is what an operator reads before approving a change, and the Discard button deletes a ref in the
instance's source of truth. Both asked git about the canonical the way one asks about one's own checkout: by short
names, in the caller's environment, decoding what came back. The canonical is a repository the network service
writes into, and each of those habits let something *in* the repository decide the answer.

- **Refs are named in full, read once, and compared as ids** (new `scripts/kontroll/canon.py`: `read_ref`,
  `proposals`, `is_ancestor`). Beside a tag called `main` the short name is the tag: the list compared each
  proposal with the tag's commit, showed no change or another commit's, and printed `heads/proposed/<id>` as the
  run id to paste. The list now carries the proposal's full ref name and full commit id.
- **A proposal is an ordinary ref that names a commit.** Anything else under `refs/heads/proposed/` — a name the
  service does not mint, a symbolic ref, a ref that names a tag, a name git gives two values — is listed **apart**
  ("Not proposals") with no control. The discard refuses such a ref as "no such proposal" and deletes with
  `--no-deref`: what is deleted is the ref of that name, never a ref it points at.
- **Every git of the list and the discard runs in one environment** (`canon.git_kwargs`): the repository is named
  outright, no other config scope is read, nothing of the caller's environment is inherited but the program path,
  the temp directory and the locale, and no hook, replace ref, graft line, `shallow` file or commit-graph of the
  repository is consulted. A repository that holds a `commondir` file, or whose `refs`, `packed-refs`, `logs`,
  `objects`, `HEAD` or `config` is a symbolic link, is refused, and the panel says so: git reads refs through
  them whatever the environment says.
- **What git returns stays bytes until it is shown.** A file name, an author or a subject that is not UTF-8 raised
  on the way to the panel and took the whole list with it. Each is now listed in an escaped spelling; a **file
  name** that is not shown as written — not UTF-8, or holding a newline, an escape or a direction override — is
  marked on the row (an author or a subject is escaped without a mark).
- **A row says what the proposal changes:** one status and one name per file, each on a line of its own; a moved
  file at both ends (`D` old, `A` new — the list used to show the new name alone); a changed submodule entry
  whatever the repository's config says about ignoring them; and against the commit the proposal was staged on,
  so a stale proposal no longer shows `main`'s later changes turned inside out.
- **What could not be read is said.** A change list git could not print is flagged on the row ("could not be
  read"), and that row — like one whose fast-forward question has no answer — gets no promote recipe. A `main`
  that is missing or is not an ordinary branch, a ref git left out, and a canonical that could not be read are
  each a warning in the panel, where they were an empty list or a row that looked fine.
- **The discard gives up on a held lock by itself** (about a second) instead of being killed over it and leaving
  the proposal's own lock file behind; its 409 says the canonical may be locked or not writable.
- **Newest first is by the committer's clock**, not by the text of the date (two proposals staged from different
  UTC offsets sorted wrongly).
- **The promote play's run-id assert is a full match** (`regex(…, match_type='fullmatch')`), the same rule as the
  Python validator: `match` with `^…$` also accepted the id followed by a newline. Its commit preview names
  `refs/heads/main` in full.

`GET /api/pending` gains, per proposal, `changes` (`[{status, path, exact}]`) and `changes_read`; and at the top
`unrecognised` (`[{ref, sha, why}]`), `note_kind`, `main_note` and `skipped_note`. `ref` is the full name and
`sha` the full id (they were `proposed/<id>` and an abbreviation); `paths` is still the list of names, now with
both ends of a move. The discard's `ref` is the full name too.

**Scope:** the Pending list and the discard, and the promote play's assert and preview named above. Needs git
2.31 or later.

**On a box that runs the previous build:** nothing to do. The Pending panel lists a moved file under both names
and shows commit ids in full.

**New here:** `tests/unit/test_canon.py` — the run-id rule's spellings fed the same samples; refs read by full
name beside every short name that would win; a test with a control (the host's plain git IS fooled) for each entry
of the environment a planted thing can be shown against, and one test of the list itself for the entries no verb
used here can show; the refusals of the seam; bytes to text; and the new pin `assert_git_runs_in_the_seam`
(`tests/unit/_readonly_pins.py`), proven against one planted call per way around it. `tests/unit/_canon_repo.py`
— the throwaway canonical those tests plant things in (tests/README.md). In `test_pending.py`, `test_discard.py`,
`tests/integration/test_gui_api.py` and `tests/e2e/test_pending_changes.py` — the list over names, authors and
subjects that are not text, both ends of a move, a stale proposal's own change, refs listed apart, what could not
be read said as such, a symbolic "proposal" refused with `main` intact, no hook and no stray lock on a discard, and
the panel painting all of it as text.

### fix(capability): a planned module text must read back as the change that was asked for — a shape the line writer does not model is a refusal, not a quiet loss (2026-10-13)

The capability dialogs edit a class's `module.yml` by line, so that a hand-written file keeps its comments. Twice
that surgery met a file it did not model and wrote something other than the plan said — a key line that carries a
comment, a list at the key's own column (the entry above) — and each time nothing noticed: the no-drop check
compares the plan, not the text. There are more such shapes. A comment line at the left margin between two
entries ends the block early for the writer, and the changed entry is then written a second time; a quoted method
or key, a space before the colon, or a byte-order mark in front of a first-line key is not matched at all.

- **Every plan is read back before it is a plan** (`_blockwrite.require_reads_back`, in the telemetry, logging and
  backup planners): YAML must read the text the writer returned as the file before with exactly the planned change
  — every other top-level key as it was, the block's key there once, the list with the rendered entry in the place
  of the one it replaces (or appended; a method the list holds twice is refused, since the dialog shows one entry
  and the writer changes the other), the mapping as rendered. If not, the answer is 409 `repository_data`,
  naming the block and the rule; nothing is written and no token is given.
- Nothing changes for a file the writers handle: every shipped class file, and a file as an onboard writes it,
  pass for every edit the dialogs can make.

**New here:** in `tests/unit/test_blockwrite.py` — the read-back over every shipped class file and over an
onboard-written one, for each edit, declared block or not; each shape the writers do not model refused, with
nothing of the file in the sentence; each clause of the post-condition on its own; the comparison by type and by
order; a file with no document; a method held twice. In `tests/unit/test_observe.py`, `test_logsvc.py` and
`test_backup.py` — each planner refusing before there is a plan, the dashboards insert included, and a logging
reconfigure reading back in place; in `tests/integration/test_api_capability.py` — the route answering 409 with
its audit line.

### fix(capability): the block writer edits a module file as it is written — a key line with a comment, a list at the key's column — so a reconfigure no longer drops or breaks a class's methods (2026-10-13)

The capability dialogs (telemetry, logging, backup) change a class's `module.yml` by line surgery, so that a
hand-written file keeps its comments. The writer found a block by looking for the bare `key:` line. A key line that
carries a comment — `metrics:      # ORDER = …`, as the shipped `docker_host` class's does — was not found: the
entry was appended as a **second** top-level `metrics:` block at the end of the file, and YAML reads the last of
two. The class's other methods were then gone from every generated file (for `docker_host`: its `host_node`
target) at the next regeneration, while the module file still showed them and the proposal's diff showed an added
block and nothing removed.

- **The key line is found with or without a comment** (`_blockwrite._block_start`), by the list writers and by the
  mapping writer; the mapping writer keeps the key line as it stands, comment included. The backup dialog's first
  declare goes through the same writer, so a `backup:` key with nothing under it is filled where it stands.
- **A list is edited at the indent it is written with.** A list whose items sit at the key's own column — what
  `yaml.safe_dump` writes, and so what an onboard writes for a class with a derived floor — or four spaces in was
  taken to end at its first item: the new entry was put above the list at an indent of its own, and the plan held a
  module text that is not YAML. The block now runs to the next key, and a new or replaced entry takes the indent of
  the entries beside it (`set_list_block`, the Settings view's list writer, shares the bound).
- **Two shapes are refused instead of written beside** — 409 `repository_data`, with a sentence that names the key
  and the rule, before anything is written: a block key with its value on the key's own line (`metrics: []`, a flow
  list — not a block the writer can edit), and a module file that already holds the key twice.

**On a box where a capability of a class with such a key line was changed through the API or the GUI** (of the
shipped classes: `docker_host`), the instance's copy of that module file may hold the key twice. Look, in a
checkout that is level with the canonical's `main`:
`for k in metrics logs backup dashboards; do grep -c "^$k:" modules/*/module.yml | grep -v ':[01]$'; done`
prints each such file, and `yamllint` — which `tests/validate` runs — reports it as `key-duplicates`. YAML reads
only the last block, so what the generators render is the last block alone. Merge the entries of the earlier
block into it by hand, regenerate (`python3 scripts/gen-observability.py`, `gen-logging.py`, `gen-backup.py`),
commit the module file with the regenerated files, bring that commit to the canonical's `main` the way the node
promotes, and deploy. Until the merged file is on `main`, a change to that block through a dialog is refused
with the sentence above; a proposal staged before the merge still holds the damaged file — discard it and
propose again.

**New here:** in `tests/unit/test_blockwrite.py` — a key line with a comment opening the block for each writer; the
mapping writer keeping the commented key line; only the key itself, at column zero, opening the block; both refused
shapes at each writer, with nothing of the file in the sentence; a list at the key's column, as an onboard writes
it, and one four spaces in, edited at their own indent; and every shipped class file put through a rewrite of each
of its entries, an added one and a replaced `backup` block, with everything else still read by YAML as before. In
`tests/unit/test_observe.py` — the planner over the shipped `docker_host` file; in `tests/unit/test_backup.py` — a
`backup:` key with nothing under it filled where it stands, and `backup: {}` refused; in
`tests/integration/test_api_capability.py` — the route answering 409 with its audit line.

### fix(fleet): a fleet line that is not a class key is skipped by what renders and refused by what installs (C22) (2026-10-13)

`instance/fleet.yml`'s `enabled_modules` is a hand-editable list, and every deploy-time reader formatted each entry
straight into a path. A line that is not a device-class key — an unquoted `no` (YAML reads a boolean), a typo with a
slash, a mapping left by a merge — was a traceback in some generators, was **accepted as a class** when it climbed
out with `..` and back in to a real one (the generated target was then planned under a path with `..` in it), and in
the generator that decides what is installed it silently took a class's collections out of the lockfile. No request
can write such a line (the one writer re-checks its key); a hand edit or a promoted proposal can. It closes C22's
known-gap clause "the deploy-time generators … and `bootstrap.yml` still join a raw `enabled_modules` entry into a
path".

- **One classifier.** `paths.fleet_problems(fleet)` is the complement of `paths.enabled_keys(fleet)`: one message
  per line that is not a class key, naming its **position** (its place among the list's entries, counted from one;
  comment and blank lines are not counted) and the
  rule, never the entry — also for a list that is a string, a mapping or a number, and for a file whose document is
  a list or a word (an empty document is, as before, a fleet with no class). The message for an entry YAML did not
  read as text — an unquoted `no`, `~`, a number, a date — says that, instead of a charset the line visibly
  satisfies.
- **What renders, skips — and says so.** The generators of the scrape targets, the log drop-ins and the Loki
  retention file, the portal tiles, the backup schedules, the scrape jobs, the exporter fragments and the
  secret-env manifest, and the checks `gen-class-capabilities.py` and `gen-validate-live.py`, take their keys
  from `paths.enabled_keys` and print `<generator>: ignoring: instance/fleet.yml: enabled_modules entry N is not
  a device-class key …` on stderr. They do not stop: some of them run inside the API and the GUI after a
  capability promote, where a `sys.exit` ends the worker after the module file was written.
- **A file with no list is not an empty fleet.** A fleet file that is not a mapping, or whose `enabled_modules` is a
  string, a mapping or a number, is refused by what installs and is NOT rendered by what renders: each renderer says
  so, returns non-zero without exiting, and writes and prunes nothing. A capability change made through the API or
  the GUI over such a file is refused before it writes anything (409 `repository_data`, with its audit line),
  because the regeneration that would follow it would render nothing. (An empty list, or none, is a fleet with no
  class, as it always was — and so, still, is an empty file: "Not changed" below.)
- **What installs, refuses.** `gen-requirements.py` stops on such a fleet with one line that says the position
  and what ends the condition — at bootstrap, at a deploy that builds its images locally, and at the **promote
  gate**, which keeps the line out of `main`. `--all-modules` ignores a directory under `modules/` that is not a
  class key.
- **The promote gate's words.** It judges the whole prospective tree, so with a bad line already on `main` it
  refuses every promote except the one that removes the line — and it used to say "Re-pin the conflicting unit"
  whatever the generator had refused. It now says that the proposal's tree does not resolve and prints the
  generator's own sentence; and when `main` as it stands is refused as well, it says that the fault is on `main`
  already and names the two ways to correct it there. `--skip-conflict-check` still overrides it.
- **The promote gate judges the ref the fast-forward takes.** It read the proposal by its short name,
  `proposed/<run_id>`, which git resolves to a tag, or to a ref directly under `refs/`, before it resolves it to the
  branch — so with such a ref beside a proposal the gate judged one tree and the fast-forward took another. It reads
  `refs/heads/proposed/<run_id>` now. Older than the fleet rule.
- **The plays run one check first, and stop on it**: `python3 -B scripts/gen-requirements.py --fleet-check`, before
  anything walks the list — in `bootstrap.yml`, before its own `lookup('file', modules_dir ~ '/' ~ item …)`; in a
  deploy twice: in `local-canonical.yml`, before the blanket commit and the mirror (a way into the canonical's
  `main` that passes no promote gate — a hand-edited line would otherwise be on `main` when the deploy ends, and
  refuse every promote; this is where an ordinary deploy stops), and again in `deploy-stack.yml`, before the first
  generator, for a run that starts later in the play. **That a deploy stops is the careful default; whether it
  should only warn is a decision for the operator.** A deploy that went on would drop the class from files the
  running stack reads, and on a node that builds its images would stop anyway at the collection lockfile, after
  those files were rewritten. The check takes no other mode beside it, and a fleet file the parser gives up on, or
  one that cannot be opened, is one line from the collection generator — with the place, when the parser gives one
  — not a traceback.

**On a box that runs the previous build.** A fleet whose every line is a class key meets nothing new: no
generated file changes by a byte (every generator's `--check` over the shipped example is unchanged). A fleet
that holds a line that is not one — including a class directory you made by hand with a capital letter or a dot,
which rendered until now and which the Fleet index never listed (the Settings view does) — now stops the
bootstrap and the next deploy (before anything is committed to the canonical or regenerated) and, because the
previous build's deploys have already mirrored that line to the canonical's `main`, every promote until it is
corrected there. To know before the first deploy of this build, run its own check from the node's checkout:
`python3 -B scripts/gen-requirements.py --fleet-check` (the previous build's generator does not know the flag and
would write the lockfile). The way out for a hand-made class is a rename, in the checkout, of everything that
carries the key: the directory, the `key:` in its `module.yml`, the fleet line, its `device_trust.<key>` entry in
`instance/instance.yml` if it pins a CA (a pin left under the old key stops applying without a word), and the
`target.device_class` of any app-store unit that names it; then deploy — the deploy's commit-and-mirror takes the
correction to `main`, and the generators write the class's files under the new key and prune the old ones. A
backup schedule Semaphore already holds under the old name (`backup-<old key>`) stays until you delete it there;
logs stored so far keep the old `device` label.

**Not changed, and written down (SECURITY.md C22 and C10):** a line that IS a class key and has no class directory
is still skipped without a word by every generator; a fleet file that is EMPTY — no bytes, comments only, `~`, `[]`
— is still a fleet with no class: it passes the check and the promote gate, and every renderer prunes what it
generated; the Settings view still lists the fleet as it is (its own pull request); the pushes into the canonical's
`main` other than a deploy's — `scripts/update.sh`, `scripts/update-from-bundle.sh`, the operator CLI's onboard —
ask no check; and the promote gate still reads a proposal as `git archive` exports it, and the fast-forward resolves
the ref again after the gate has judged it — a proposal's own `.gitattributes` can hide its overlay from the gate,
and a push between the gate and the fast-forward is promoted unjudged (SECURITY.md C10; the next change to that
gate).

**New here:** `tests/unit/test_fleet_line.py` — the classifier (positions, never the entry; `enabled_keys` and
`fleet_problems` split the list between them; a line YAML read as another type is told so); every generator's
fleet-walking function over a table of lines that are not keys — equal to its answer for the clean fleet, and asking
the filesystem about nothing else; every generator's entry point saying which entries it skipped; each renderer
rendering nothing for a file with no list — and, in its bare run, pruning nothing; the in-process regenerations (observability, logging, backup) run for real against
a fleet with such lines; a real run of the collection generator in each of its seats — a bad line, a file the
parser gives up on, a file that cannot be opened, the check beside another mode — and the plays' check writing
nothing; a capability change over a file with no list refused before it writes; a real promote over a throwaway
repository — a proposal that adds the line; with the line already on `main`, an unrelated proposal and one that
edits the fleet file elsewhere refused with a sentence that says the fault is on `main`, a proposal with a fault of
its own refused without it, and, on a bare canonical, the proposal that removes the line promoted; and the check
task of `bootstrap.yml`, of `deploy-stack.yml` and of `local-canonical.yml`, by position, each stopping its play
and each run as `python3 -B`. `tests/unit/test_kontroll_promote.py` gains the gate judging the branch when a tag,
or a ref directly under `refs/`, carries the proposal's short name. **A guard was edited:** `tests/unit/test_deploy_stack_check_mode.py` holds every `scripts/gen-*` command that
is not a read-only mode to stay skipped under `--check`; `gen-requirements.py --fleet-check` has to run in the
dry-run and is now such a mode — named together with the script that implements it, as `gen-image-digests.py --env`
now is. A command counts as read-only there only as one generator, on one line without shell syntax, whose words
after the script are exactly `--check` or that script's listed mode.

### docs(security): `signature: required` is a declaration — C15-a says what it does today, and a warning stops saying the opposite (2026-10-12)

No install behaviour changes. Several texts said that a collection's `required` policy fails the install when no
signature is served: C15-a ("`required` (fail-on-absence) is an opt-in ratchet"), the comment in the unit schema's
example (which also said `none` drops the signature layer), and a warning `gen-requirements.py` prints for a
fleet-wide `signature_policy: required` ("this FAILS every install"). None of that is so.
`scripts/install-collections.py` builds one command line whatever the policy says — with a keyring file,
`--required-valid-signature-count all` and the two ignored status codes, never the `+`-prefixed count that fails
on absence; without one, a plain install. The policy is written into the trust sidecar, counted in the wrapper's
summary line and shown by the provenance view; nothing else reads it. Found by the read of the previous entry's
change, when its first fix leaned on the word.

- **C15-a now says so**, and says what follows for the **Tier-cap**: "a unit aimed at the edge firewall or the
  core switch must set `signature: required`" is a rule about what a descriptor *declares*, checked in
  `tests/validate` and by the create route and not by bootstrap, deploy or promote. A unit written outside the
  create route that declares it passes the cap, and its collection — unsigned, on public Galaxy — installs like
  any other. The create route does not lean on the word: it takes no signature, writes `adaptive`, and refuses a
  class on those two groups with 403. Also written down: the cap does not ask which collection a unit installs,
  so a unit aimed elsewhere may pin one a guarded class uses. All of it is a known limit that nobody has accepted;
  whether such a unit may reach those two groups before `required` is enforced is a decision still open.
- **The provenance view paints `signed`** for a collection that is declared `required` while a keyring file is
  held. It has not seen a signature. C15-a says that too; the view itself is not changed here.
- **The signature layer is described as what it is: dormant.** No keyring is provisioned, so no install has run
  with the signature flags; C15-a no longer says an unknown key is tolerated or that a keyring is auto-provisioned.
- **The generator's warning** for a fleet-wide `required` now says what the setting does (it is recorded, not
  enforced, and a keyring file turns the view green) instead of predicting a failure that does not happen. The
  line is printed to stderr; nothing parses it.
- The two "un-defer trigger" sentences say that the ratchet has to be built, and C15-a's residual no longer says
  the Tier-cap keeps the edge firewall and the core switch out of it.

### fix(actuation): a unit descriptor's collection and name are checked where the descriptor is validated (C22) (2026-10-12)

`validate_unit` asked of `unit.collection` and `unit.name` only that they be there. The create route checked its
own two inputs, so nothing a request could author was affected — but a hand-written `unit.yml` passes no route, and
the schema called valid a collection with a line break in it, a name that was a mapping. Every reader of a
descriptor (the would-run preview, the registry's list behind the Automations section, and the generated template
and role wrapper that are not emitted yet) was left to defend itself against fields the validator had passed.

- **`unit.collection` is a collection name** (`namespace.name`, lowercase — `paths.is_collection_fqcn`, the boolean
  twin of the request validator) **and `unit.name` is one word** (lowercase letters, digits and `_`, at most
  sixty-four characters — `_actuation_schema.UNIT_NAME_RE`), for the kinds that install something. A `standalone`
  unit needs neither and is not asked — also not when it carries one anyway. The messages name the field and the
  rule, never the value.
- **One rule, two consumers.** The create route's `name` check uses the schema's pattern instead of one of its own,
  so what the route accepts as a name is what the schema accepts. That brings **one new refusal at the route: a
  unit name longer than sixty-four characters** is answered 422 on `name`.
- **A collection and a name that are each valid but too long together** made the create planner *raise*, once the
  collection's class had been found, enabled and off the guarded groups: the derived key is a directory name, and
  the path builder refused it as a request field called `unit key`, which no request carries — a 422 with the code
  `invalid_unit_key` and a `request-refuse` row in the audit log. The planner now answers it itself — `invalid`, on
  the key, before the class is looked up and before a path is built: a 422 without that code and, like the
  planner's other field errors on a dry run, without an audit row; and it is the answer whatever the fleet holds,
  where a class that was not enabled or sat on a guarded group used to be named first. (Measured on the tree
  before this change: `community.docker` with a sixty-four-letter name raised.)
- **The example in `actuation/README.md` was not a valid unit**: it aimed an `adaptive` signature at the core
  switch, which the Tier-cap refuses and always has. The example is now the unit the create flow itself writes — a
  class on a group the cap does not guard, `adaptive` — and a test runs the page's example through the validator.

No shipped unit exists, and the fixtures the suite builds all pass. A hand-written descriptor is refused by
`gen-actuation.py --check` (and so by `tests/validate`) where it validated before when its name carries a hyphen,
a capital, a dot or a space, is longer than sixty-four characters or is not a string (an unquoted `123` or `yes`),
or when its collection is not exactly `namespace.name` in Galaxy's lowercase form. Two shapes Ansible itself can
address are among them and cannot be a unit: a collection playbook whose file name has a capital, and one in a
subdirectory of the collection's `playbooks/`. The validator runs there and as the create route's last check, and
nowhere else: bootstrap, deploy and promote do not read these two fields, and the API and the GUI load a
descriptor without it, as they always did — the preview keeps quoting what it reads (C22 lists the limit). Still
open and listed in C22: a unit may target `all`, `ungrouped`, `localhost` or no group at all — that change to the
Tier-cap waits for a decision.

### docs: name the condition, not the default — the privileged tier's sentences say `api_privileged`, and one about the canonical's mount is corrected (2026-10-12)

No behaviour changes: documents, comments and docstrings.

Many sentences described the privileged tier by its **default** — "deployed read-only by default", "(default
off)", "read-only until armed", "the flag default is false", C9's "Deployed read-only", the "(gated; default
off)" in the headings of C10 and C11. A default is a fact about one `deploy-stack.yml`;
the rule those sentences are about is a **condition**: without `api_privileged` no token is rendered, the content
clones stay root-owned, the canonical is mounted `:ro`, and the privileged routes answer 503. They now name the
condition, and the default is stated once — [privileged-mutation-enablement.md](docs/privileged-mutation-enablement.md)
§3 says which value the public tree ships and names the `api_privileged:` line of the deploy play that sets it. A
sentence written this way stays true on an edition whose deploy play ships another default, so where the
permissive edition has nothing to add, it can carry the public line unchanged.

- **Corrected, because it was not true:** [trust-mode.md](docs/trust-mode.md) and SECURITY.md C10 said that in
  both postures "the canonical stays `:ro`" to the network service, and listed "the `:ro` RCE backstop on the
  service side" among what bounds `solo`. Staging needs the privileged tier, and with it armed the canonical is
  mounted `:rw` into the API, the GUI and the runner — what keeps the service's pushes off `main` is
  `commit_and_push` and the canonical's `update` hook (it refuses a uid-1001 push to `main`), and C10's own
  "Arming …" paragraph already said that neither binds a container RCE, which can then write the canonical
  directly. Both places say so now; C8's "the runner mounts it read-only" gains its condition.
- **"A leaked token can only park proposals you can reject"** is a statement about `separated`. What holds in
  every posture is now said without a qualifier — a token can only *stage*, it cannot promote, and the API holds
  no key to decrypt a secret with — and what `separated` adds (each proposal waits for you to reject or promote
  it, so the token alone never rewrites the lab) is said as that, with a pointer at `docs/trust-mode.md` for
  `solo`: in C10 (the rule, its residual, the `/actuation` paragraph, the accepted-risk row), on the enablement
  page, in the install walkthrough, in the GUI's README, and in the docstrings that state the property
  (`gitio._push_target` and the tests of the staging push).
- The headings of C10 and C11 no longer carry "default off" (the one in-page link to C10 now carries the
  heading's full fragment; the short one it had did not resolve), and the pointers at the
  permissive edition's own records — in the decision-record index and on the review-process page — name its
  tree instead of "there".

Not changed, deliberately: the sentences that say **this** edition has no auto-promoter. They are written in the
present tense and held by a test (`tests/unit/test_trust_mode.py`), and the page a `solo` operator reads must say
what this tree does.

### fix(service): a name the server takes on is held to its field's rule — and a file that fails is not a refused request (C22) (2026-10-12)

Onboarding a second device of an existing class ADOPTS that class's inventory group, secrets domain, backend and
role from its module file. The request's own fields were checked; the file's values that replaced them — and the
role, which no request carries — were not. The adopted domain is formatted into a lookup expression in every
credential host var, the role is a host var Ansible templates, the group is the drop-in's top-level key.

- **Each adopted name the plan takes is held to the rule of the request field of the same meaning, where it is
  taken** (`onboard._adopted`), at the plan and before any write. A credential field's `maps_to`, the NAME of
  the host var a value is written under, is an identifier, so it cannot carry a brace, a space or a line break,
  and an `inline` field must name one. WHICH host var a class's credential field writes is still the module
  file's to choose (`ansible_connection` is an identifier).
- **One request could store a name the next one refused.** The identity knobs that hold a name had no length
  bound; the path join stops at sixty-four characters. An eighty-letter secrets domain was accepted and promoted,
  and the next onboard of that class answered 422 `invalid_secret_domain` — a field the caller never sent —
  **after** the inventory drop-in had been written. The knobs are bounded like the join now. So is the role an
  onboard or the scaffold gives a new class — the backend registry entry's own, or the one derived from a
  backend no entry names a role for (`backend_<name>`): a backend too long to derive a name from is refused as
  the `backend` field when the request sent it, and as repository data when a file did. Tests hold both as
  properties: no word the identity knobs accept is refused by the adoption rule, and the role a new class's plan
  or the scaffold writes — the registry's own or a derived one — is a role the next onboard adopts.
- **A file that fails is answered as a file that fails.** `paths.RepositoryDataError` — the repository's own data or
  layout stopped the request: a module file's adopted name (or, at the onboard and at an identity edit, a module
  file that is not a mapping, which was a 500 there — the other readers of a module file, the capability dialogs,
  the Fleet index, classify and the cred-fields route among them, still answer 500 for such a file), a credential
  field's declared name, a backend that names no role and has a name none can be derived from (or whose registry
  entry holds a role that is not a name), a write target that does not resolve inside the repository. Both apps
  answer **409** `repository_data` with a sentence that says where to look and which rule (never the value) and
  audit `repository-refuse what=<kind> route=<route>`. Before, the ones that were refused at all — a write target, a
  module file's domain when a credential was being stored, a credential field's name — were a 422 with a `code`
  naming a field (`invalid_path`, `invalid_secret_domain`, `invalid_credential_field`) and an audit line recording a
  refused request; an adopted group, backend or role was not looked at. `apply_onboard_plan` asks about the drop-in
  targets it is going to write before its first write.
- **`all`, `ungrouped` and `localhost` are refused as a device class's group** wherever one is chosen — the
  onboard field, the identity knob (a GUI credential could re-home an existing class onto `all` with one staged
  edit), the scaffold, an adopted group — **and, with `127.0.0.1` and `::1`, as the NAME a request gives a host
  in the inventory** (`host_name`; the address is not a name). A class's group is a play's pattern: `hosts:` in
  the logging wiring play and a unit's play, the limit of the backup schedule. Measured with throwaway
  inventories and ansible-core: a group named `localhost` makes `hosts: localhost` address that group's hosts; an
  inventory HOST named `localhost`, `127.0.0.1` or `::1` — Ansible's own names for the control node — replaces
  the implicit one, so every play written for the control node runs against the device; and with a host named
  `all` the pattern `all` addresses that one host. A group that is only read back — by a generator or a play —
  is never refused.
- **`galaxy.py scaffold`** holds `--key`, `--group`, `--secrets` and `--backend` to the planner's rules (a
  `--key ../../x` used to write outside the repository), writes under the write root, not the code root, and
  refuses a key whose class the code root ships (a stub in the write root would shadow it);
  **`galaxy.py openapi --name`** is one path component. **The onboard's apply** gives the same answer where the
  write root is not the code root and does not hold a class the code root ships (a clone older than its image):
  under that class's key, for another collection, it is the write conflict it already was wherever the write root
  holds the class, before anything is written (it used to write a module that shadowed the shipped class for
  every reader; `overwrite` remains the deliberate way).
- The cred-fields route of each app checks the secrets domain it answers with. A reuse-onboard's commit body —
  on both routes and from `galaxy.py onboard` — names the group the plan ADOPTED, not the one the request sent,
  and the command line prints the adopted secrets file. An option of an installed module whose name is not an
  identifier is no longer offered as a credential field, and a module with a required or `no_log` option of that
  kind is not used for the exact set at all (it used to refuse the onboard that asked for `deep`). A declared
  `auth:` entry whose `field` or `kind` is not a string is skipped like any other malformed entry, one whose
  `auth_set`, `group` or `help` is not a string is used without it, and — at the onboard — a `collections` block
  that is not a list declares none (each was a 500 there; the cred-fields route still answers 500 for such a
  block).

**Before you upgrade — what an instance that already exists can meet.** Nothing below affects a deploy, a
generator, a scheduled run or a capability dialog of a healthy tree: only an onboard, the identity dialog, the
command-line verbs named last, and a client or a query that keyed on the old codes.

- **A module file whose group, secrets domain, backend or role is not a name by the onboard's rule** — lower-case
  letters, digits, `_` and `-`, at most sixty-four characters, starting with a letter or digit, and not a word
  YAML reads as another type. A hand-edited `inventory_group: Core_Switch`, a group or a role with a dot: the
  second onboard of such a class used to succeed and now answers 409 until the file is corrected; the answer
  names the class, the key and the rule. Renaming a **group** re-homes the class: move its hosts under the new
  key in `instance/inventory/onboarded-<key>.yml`, rename its `group_vars` file if it has one, regenerate
  (`gen-observability.py`, `gen-logging.py`, `gen-backup.py`, `gen-homepage.py`) and dry-run the deploy.
  Renaming a **role** means renaming the role directory too. An empty string, `0` or `false` used to mean "not
  stated"; only an absent key or `null` does now. No shipped module file is affected.
- **A class that already sits on `all`, `ungrouped` or `localhost`**, or whose file holds a name longer than the
  bound, keeps deploying and keeps its dialogs: the identity dialog does not judge a value it reads back
  unchanged, so its other keys stay editable. Its next onboard answers 409 until the file is corrected.
- **Codes.** `invalid_path` and `invalid_credential_field` are no longer sent by anything. `invalid_secret_domain`
  is still the 422 of the secrets routes' own `domain` field; an onboard no longer sends it. `POST /onboard` now
  has more than one 409: a script that treats every 409 from it as "the file exists, retry with `overwrite`"
  must look at `code` — `repository_data` is not a conflict and `overwrite` does not help. (The page keys on
  `conflict`, not on the status.)
- **Audit.** A query for `request-refuse` or `onboard-refuse` no longer returns these causes; they are
  `repository-refuse what=module file|credential field|backend|path`. On an apply, an `onboard-apply` line can
  now be followed by `repository-refuse` and no `onboard-result`: nothing was written.
- **A tree whose `instance/` (or `modules/`) is a symlink out of the write root.** It is still read. An onboard
  apply of a new class and an app-store unit were already refused there (422 `invalid_path`, after the module file
  had been written); they answer 409 now, before anything is written. An onboard's preview still succeeds — its
  refusal comes at apply; an identity or capability preview is refused already. The remedy is the layout: make the
  directory a real directory of the write root (a bind mount is one; a symlink is not), or point
  `KONTROLL_WRITE_ROOT` at the tree that really holds it.
- **`galaxy.py scaffold --key/--group/--secrets/--backend` and `galaxy.py openapi --name`** refuse a capital
  letter, a dot, a space and a leading underscore (`--name Radarr`, `--name radarr.v3`). A name derived from the
  specification's title is unchanged. **`host_name: localhost`** (or `127.0.0.1`, `::1`, `all`, `ungrouped`) is
  refused; a host that already has such a name in an inventory is not touched, but onboarding it again under
  that name is refused — give it another name and remove the old entry by hand.
- **An install whose write root is apart from its code root, where the write root lacks a class the image
  ships** (the clone was made from a canonical older than the image, or the class's directory was removed from
  it): an onboard under that class's key for another collection used to succeed and replace the class for every
  reader; it is the 409 conflict now that it already was where the write root holds the class — the usual case,
  since the clone is made from the canonical and the canonical carries `modules/`. If it happened on your
  instance, `modules/<key>/module.yml` in the write root names another collection than the shipped file of that
  key does.

An unused helper, `onboard._existing_groups`, is removed. Still open and listed in C22: a unit may target a
reserved name or no group (its own pull request); a parent group the inventory defines is a group like any other
to the class seams; which host var a module file's credential field writes is the file's to choose.

### docs(security): C22 says which writes are confined, and its known gaps say what is true (2026-10-12)

No behaviour changes. Control C22 of `SECURITY.md` ends with a list of its own known gaps; the design run for
closing them found that the list, and one sentence above it, were not exact.

- **"Every repository write goes through `paths.confined()`" was not true.** The drop-in writers do — a module
  file, the inventory drop-in at onboard, a unit descriptor and its values. Nothing else a request can make the
  server write passes it, and the control now names each such writer and how it builds its path: a fixed path
  (the fleet file, the instance file, the capture-exception file, the recipients file, the Homepage board's two
  files, the generated backup-schedule and Loki-retention files, the matrix cache); a path whose one variable
  part is a checked name (a host-vars edit, a secret domain); and one kind that is checked nowhere — the
  Prometheus target files and Vector fragments a telemetry or logging change regenerates inside the worker,
  named after a line of the fleet file as it stands (the raw-fleet-line gap, already listed). The control's
  heading says "drop-in writes are confined" accordingly; the same claim is corrected in a comment of
  `paths.py` and in a test's docstring. The writers are unchanged.
- **Written down, not built:** nothing compiles the Vector fragments `gen-logging.py` writes before Vector loads
  them (the gate checks them for freshness; `vector validate` reads the hand-written configuration only — the
  generator's docstring, the logging README, the logging architecture page and a lint exclusion called that
  "the semantic gate", and no longer do); a unit with NO target group passes the Tier-cap and its play addresses
  every host; the cap compares group names, so a parent group an instance defines over the edge firewall or the
  core switch is not seen (C15-a now says so, and what would close it); in a hand-written descriptor a unit's
  `collection` need not be one its `install.collections` pins; `kontroll-init --fresh` writes `--mgmt-ip` and
  `--domain` as given; `kontroll-promote.py`, run by hand, takes its `run_id` with no grammar; and
  `yamlscalar.plain_scalar` is a rule about how YAML reads a word — a bidirectional control or a no-break space
  is a plain word to it, which no request field held to the rule admits today (a capture exception's free text
  is not held to it at all).
- **Moved:** "a lookup that finds nothing quotes the unvalidated value it was asked for back to its sender"
  belongs with the work on error bodies, and the paragraph says so.

The gaps that remain in the paragraph are closed in the pull requests that follow, each deleting its own clause.

### docs: one written install sequence — every other copy points at it, and every scaffold command names its trust mode (2026-10-12)

The first-install sequence stood in many places — the quickstart, the README, `docs/SETUP.md`, the VM recipe, the
installer's README, the scripts' closing messages and their usage headers — and the copies had drifted. After the
scaffold gained its required `--trust-mode` flag, every copy but the quickstart showed a command that is
**refused at step one** (on the permissive edition the same line is not refused: it silently arms the box). The
quickstart at the top of `docs/install-from-scratch.md` is now the one written sequence. It says how the tree
gets onto the node (a clone, a launch kit, or a release bundle — whose scripts are inside it), that a first run
of `install-prereqs.sh` may need a re-login, and it ends where the default install ends: Semaphore and Homepage.
The onboarding GUI stays a separate, deliberate step in §7 — its key grant and the arming deploy are not part of
the default block. Everything else points there instead of repeating it: the README, `docs/SETUP.md` §1, §2, §4
and §6, `docs/bootstrap-control-vm.md`, `docker/installer/README.md`. **What a script prints changes:**
`install-prereqs.sh`, `install.sh`, `make-launch-kit.sh`, `make-bundle-airgap.sh`, `make-bundle.sh` and
`kontroll-init` each end with ONE next command and that pointer, and every usage header and comment that shows a
scaffold command carries `--trust-mode separated`. The quickstart also states, once, how a control-plane play is
run later on the compose-native path (`scripts/kontroll-installer.sh playbooks/<x>.yml <args>`; a deploy is
`init <args>`) — and that a play against fleet devices is run as its Semaphore task, because the device
collections are in the runner image and not in the installer's.

Corrected beside it, each re-found in the code: the host floor is Docker and git — Ansible, sops and age are in
the installer image, as the dependency table now says, and `./bootstrap.sh` is described as the shim it is;
`init` generates the fleet SSH key on the compose-native path; `check` on a brand-new node is a partial preview
to run BEFORE `init`, not after it; the launch-kit launcher pulls each image by digest and then does a plain
`run` (there is no `--no-build`); the installer image is published as a public package;
`configure-semaphore` registers the local canonical (`file:///srv/kontroll.git`), one template per file under
`config/semaphore/templates/` and the generated schedules, and needs no exported password; the manual
`groupadd` step is gone from the installer README; and `install.sh` no longer says an update is "install a newer
bundle the same way" — it refuses an existing directory. **Not changed:** the sentences that describe an
in-place upgrade (`scripts/update.sh`, `scripts/update-from-bundle.sh`, their section of `scripts/README.md`,
`docs/local-source-of-truth.md`, `docs/public-split.md`) — whether that path is repaired or replaced is an open
decision, and they wait for it. Proofs: `tests/unit/test_install_commands.py` (no scaffold command without a
trust mode anywhere in the tracked tree, a backslash-continued command read as one line; a closing message names
the document and does not restate the sequence), `tests/unit/test_canonical_group_provisioning.py`.

### fix(homepage): a host or class whose name contains "password" no longer stops every deploy (C11, C22) (2026-10-12)

`scripts/gen-homepage.py` refused to write the Homepage fleet tiles when any tile field contained the substring
`password`, in any case — and a tile's fields are the host's name, its address and its class key. A host called
`password-vault-1`, an address under `passwords.example.test` and a class key `passwordstore` are ordinary names;
each passes the request boundary on purpose. So **one onboard request with such a name stopped every later
`deploy-stack` of that instance at the Homepage step**, until the host was renamed — and an honest operator with
a host really called that met the same. The test existed to keep a credential off the unauthenticated board, and
a name is not one. The guard now refuses what no quoting can make safe, and nothing else: a tile field that
holds a line break, and one that holds a Homepage substitution marker. That second rule is also **wider where
it should be**: Homepage replaces `{{HOMEPAGE_FILE_…}}` with the contents of a file exactly as it replaces
`{{HOMEPAGE_VAR_…}}` with a variable's value, in the raw text before any parser, and only the `VAR_` form was
refused. A refusal names the field, the rule and the class, never the value (the old message printed the tile's
name). No request can store either refused shape, and a test holds that over every short string of the two
request charsets. Proofs: `tests/unit/test_gen_homepage.py`. SECURITY.md C11, C22. Design:
`docs/reviews/2026-10-12-request-boundary-gaps/` (kept in the private repository).

### fix(service): a staged write lands whole, or not at all — the baked reconfigure path, app-store units, a refused `git add` (2026-10-12)

Ways a change staged from the GUI or the API could fail to reach its proposal — none of them visible to a suite
that mocks git. **On a baked install** (the launch kit; `bake_code=true`) the services run from a
read-only image tree and write the propose clone, and a device class is read through an overlay — the clone's
copy when there is one, the image's otherwise. The telemetry, logging and backup reconfigures each spelled the
path themselves, against the image tree, and identity wrote to whatever path it had read. So a class the
operator had onboarded opened its dialog and was then "not onboarded"; a shipped class was planned from the
image's bytes while its current values came from the clone, so an earlier reconfigure silently vanished from the
text about to be written (the no-drop gate compares two structures that both came from the clone, and could not
see it); the write went to the read-only tree; and had it succeeded, the commit staged a path in the clone where
nothing had changed. They all read through `paths.module_file` now and write to `paths.module_write_path` —
always the write root — through one shared writer; a plan that changes nothing writes nothing in either tree,
and a module the clone does not hold is not among the paths handed to the commit. `POST /refresh` failed there
the same way: its cache path was a constant under the image tree, and is under the write root now. **An
app-store unit could not be staged on an instance built from the public tree:**
`instance/actuation/` was not re-included by the `instance/.gitignore` an instance is scaffolded with, so the
path was ignored and `git add` refused it. `instance/certs/` — where the example `instance.yml` tells you to
commit a pinned CA certificate — was not re-included either. Both are now. **And the commit seam did not read
`git add`'s status.** git exits non-zero for a path it refuses — having staged the others. With one path the
commit then had nothing to commit and the stage failed — but the refused file stayed in the clone (the
roll-back does not remove an ignored file), where the registry listed a unit that existed nowhere else. With
several paths the commit went through without the refused one: a proposal missing a file, reported as staged.
`commit_and_push` now stops on a refused add, commits nothing, and — when it is the staging service — rolls
the clone back and removes the refused files it was given. Its result carries `add_rc` in that case. A caller
that is not the staging service (the GUI run from a checkout) keeps its working tree, and the request's paths
are taken back out of its index — after a failed commit as well — so a part of one request cannot reach `main`
inside the next one's commit. **And the GUI's capability route did not read the seam's result:** for a stage
that had not committed it answered 200 and the page printed "committed to main". It answers 500 with the error,
like every other route. **One thing you will see because of it:** promoting a capability whose values you did
not change, which that route used to answer as "committed to main", now answers the same error ("commit failed;
nothing staged"). Nothing was committed in either case; the regenerators report their files whether or not those
changed, so the route cannot yet tell "nothing changed" from a commit that failed — that is theirs to say, and an
item of its own.
**For an existing instance:** add the lines `!/actuation/` and `!/certs/` to your `instance/.gitignore` (the
scaffold never overwrites an existing file). Until you do, creating or configuring a unit fails with "commit
failed", and a unit created from now on is no longer left in the dialog's list. A unit left in a service's clone
by an earlier attempt is still listed, and cannot be created again while it is there. On the control node, for
each of `<storage root>/api/repo` and `<storage root>/onboard-gui/repo` (the storage root is `/var/lib/kontroll`
unless `KONTROLL_STORAGE_ROOT` says otherwise), run, as root,
`git -C <clone> status --short --ignored -uall -- instance/actuation`: every file it lists is a leftover (a
promoted unit is tracked, and is not listed). Remove each one
(`git -C <clone> clean -f -x -- instance/actuation/<key>/unit.yml`, and the `vars.yml` beside it) before creating
the unit again. Everything you keep in `instance/certs/` shows up as untracked after the edit — look before you
commit: a certificate and a `known_hosts` file belong there, a private key does not. Proofs:
`tests/unit/test_baked_capability_promote.py`, `tests/unit/test_staged_write_lands.py` (both run the real git),
`tests/unit/test_write_root_seam.py`, `tests/unit/test_leak_guard.py`, and a route test on each surface for a
stage that does not commit (`tests/integration/test_gui_api.py`, `tests/integration/test_api_capability.py`).
SECURITY.md C10.

### test(docs): every relative Markdown link resolves — SECURITY.md's own references left the repository (2026-10-12)

The root `SECURITY.md` wrote each of its relative links as if it sat one directory down (`](../docs/…)`). From
the repository root `../` leaves the repository, so on the published page every reference the control document
makes — to the install guide, the logging architecture, the trust-mode notes — was a 404, and no gate read
links. They resolve now. The references into review dossiers that exist only in the private repository are
written as code spans, not links, and two READMEs with the same fault
(`instance.example/dashboards/homepage/README.md`, `secret-forms/README.md`) are corrected. A new validate step,
`doc-links` (`tests/check-doc-links.py`), fails on a relative link in a tracked Markdown file that does not
resolve to a tracked path inside the repository — spelled exactly, so "it resolves on my disk" (a
case-insensitive file system, a file that was never added) is not a pass — and a target that climbs out is a
finding even where a file happens to exist there. It is hermetic: no URL is fetched, anchors are not checked,
and fenced code blocks and code spans are skipped. Exempt by declaration, because the target cannot exist in
the shipped tree: `instance/…`, and a dated run folder under `docs/reviews/` that the tree does not carry. Not
read at all: `CHANGELOG.md`, the files inside such a folder, and an instance repository's own `instance/`
documents. Where git cannot list a git tree, the step fails and says so instead of reading some other set of
files. Proofs: `tests/unit/test_check_doc_links.py`.

### fix(security): the blanket commits into the canonical are gated — a plaintext secret is refused, by content (C1) (2026-10-12)

Two places commit a whole working tree into the control node's canonical with nobody reading the diff: the
deploy's "instance state" commit (`local-canonical.yml`) and — missed by the review that asked for this — the
launch-kit launcher (`scripts/kontroll.sh`), which on a published install is the FIRST capture of a freshly
scaffolded `instance/`; the deploy's own commit then finds a clean tree and never runs. "Everything committed is
ciphertext or public" was a comment in the play. A secrets file typed in an editor and never encrypted has the
right name; one plaintext value added by hand to an encrypted file still has its `sops:` block, which is all
`tests/validate` looked for; a `sops -d … > network.yml` copy, an editor backup, a break-glass key generated in
the repository directory (as SETUP.md's command did) — each was staged by `git add -A`, pushed to the canonical,
cloned from there by the runner and copied by every backup. Both commits now run
`scripts/lib/plaintext-guard.sh` first and stop, naming the file and the rule, on: a `*.sops.yml` in which a
value does not have the shape of ciphertext — before the `sops:` block, **inside** it, or after its last line —
or that has no such block; any other file in a secrets directory; any file that holds a private key (an age
identity anywhere in a line; a PEM or PGP private-key header that begins its line, or one anywhere in a line
with key material directly after it — the shape an SSH key has when it is inlined in `group_vars`; an inlined
PGP block, whose body starts after a blank line, is NOT seen).
"Inside" is the case that took several attempts: the block is the last thing in the file, so a key added "at
the end" gets the indent of the block's last line from the editor and becomes part of it; sops ignores such a
line and keeps decrypting, so nothing else notices. The block's structure is read now — its keys must be
sops's own, and nothing follows `version:`. The guard is POSIX sh with git and the base tools (awk, tr, wc,
find — the launch-kit host has no python and no sops), is read-only, runs under `--check` too, and prints
paths, rules and line numbers — never content. It runs on the HOST's awk, so its tests run under each
implementation they find (gawk, mawk, busybox awk, the one-true-awk), and CI installs them. When it cannot
look — git cannot read the directory, the directory is not the top of its repository, the list of files does
not reach the checker whole — it refuses, with a sentence saying which: a stage that dies on the way to the
checker used to hand it a short list, and a short list read as "nothing wrong". `tests/validate`'s
`sops-encrypted` step is now the same guard over the secrets directories: it needs no sops binary, and it
checks values, not a marker. A new step, `sops-guard-conformance`, holds the predicate against documents the
installed sops wrote and fails when one of its tests was skipped, so a sops release that changes its output
format fails the gate instead of refusing every healthy secrets file at the next deploy. Running it against the
pinned sops is what found three shapes nobody had guessed, each of which the first version refused: a comment
that is only `#` (left as it is), a note on a value's line (kept there, as an encrypted comment behind the
token), and an empty item in a list (written back empty).

The launcher changes in two ways besides the gate. A kit extracted inside another git repository — a home
directory kept under git — now gets a repository of its own; it used to stage and commit the outer one, and the
canonical was then seeded from that. And `./kontroll shell` skips the capture: it deploys nothing, and it is the
only way to the image's sops on a host that has none, so a refusal must not lock it out. `.gitignore` gains the
names a decrypted copy, a key, an editor backup and an env-file backup are commonly saved under — not `*.pem`:
a public CA certificate is a `.pem` an operator is told to commit, and a PEM private key is refused by its
content. The identity dialog's enact hint no longer tells the operator to `git add -A`; it stages the
generators' output directories. SETUP.md gives the break-glass key a path outside the repository.

**On an existing node:** a working tree that holds such a file — new, modified or staged — fails its next
deploy at this task, by design, with the file named; a clean tree at the top of its own repository is never
refused, and a file that is already committed and unmodified is not re-read at deploy (only `tests/validate`
reports it). A value that a partial-encryption rule in `.sops.yaml` (`unencrypted_suffix`, `encrypted_regex`)
leaves in the clear is refused like any other plaintext: the guard assumes the whole file is secret, as the
shipped rules make it. So is a secrets file with a key that contains `: `, or a key long enough for sops to
write it as `? key` — rename the key. A launch kit that an earlier launcher captured into an OUTER repository
gets its own repository on the next run; that node's canonical was seeded from the outer repository's history,
so the deploy's fast-forward-only mirror will refuse — rebuild the canonical. Not covered: a file already
committed, a commit made by hand, the API's and the GUI's scoped commits, a secret in a key name, a value typed
in the shape of a ciphertext token, a line put by hand among the recipient entries of the `sops:` block, a
`README.md` in a secrets directory, a free-named notes file elsewhere. Proofs:
`tests/unit/test_plaintext_guard.py`, `tests/unit/test_launcher_capture.py`,
`tests/integration/test_plaintext_guard_sops.py`, `tests/unit/test_gitignore_secret_shapes.py`,
`tests/unit/test_local_canonical.py`, `tests/unit/test_launch_kit.py`, `tests/unit/test_identity.py`.
SECURITY.md C1, C8.

### docs: five things the documents said that the tree does not do — and one dead link fixed with them (2026-10-12)

**One of these changes what an install renders.** The Grafana link in the GUI's Index panel
(`config/services.yml`) and the Grafana tile every fresh install's Homepage starts with
(`instance.example/dashboards/homepage/services.yaml`) said `https://<mgmt>:3002`. Grafana serves plain HTTP on
that port — the capability dialog's deep-links have always been built on `http://` — so the link and the tile
were dead on first boot, and a test pinned the wrong value. Both say `http://` now, and one test holds the Index
link and the deep-link base together. **An existing instance's own
`instance/dashboards/homepage/services.yaml` is not rewritten:** its Grafana tile keeps `https://` until you
change it (the Homepage editor on `:8443`, or the file). The Index link follows on the next deploy.

The rest is text. *"The auto-promoter ships in a later release"* stood in many places — two install
documents, `trust-mode.md`, SECURITY.md (which also gave it the installer's control number), the example
`instance.yml`, `kontroll-init`'s help and refusal, the deploy's assertion message, the Settings row. Nothing in
this tree backs that forecast: the auto-promoter is the permissive edition's code and is kept on its own branch.
The sentences this tree shares with that edition — the two install documents and the deploy's assertion message —
now say only what `separated` does and send the reader to `trust-mode.md`; the rest say what is true here: this
edition has no auto-promoter, and `solo` behaves like `separated`. `trust-mode.md` itself now says which of its
sections describe an edition that runs one.
*"Binds every privileged port to your `mgmt_ip` only (never 0.0.0.0)"* (SETUP.md) left out the two ports that
are published on all interfaces on purpose, Homepage and Semaphore — and Semaphore is where a proposal is
promoted; the sentence now names them, as SECURITY.md C3 already did, and says to firewall them on a
multi-homed host. The promote step's Semaphore URL in `install-from-scratch.md` said `https://`; it is `http://`. SETUP.md
now says which surfaces serve HTTPS in which `tls_mode`, and the arming runbook's verify command uses the scheme
the default mode serves. `api/main.py` said routes are "drop-in routers — adding a domain = adding a file, never
editing a hub" (registering one is an import and a tuple entry in that file), that the privileged onboard was
"deliberately deferred" and the API "not deployed", and its served OpenAPI description promised mutation routes
"in later workstreams"; all three describe what is there now, and a test fails for a route module that defines
a router and is not registered. SETUP.md said the deploy "does NOT mint" the GUI's TLS certificate and told the
operator to provision one; in `self_signed` mode the deploy mints it when none is there (the age key is the part
that stays yours to provision — unchanged). No break-glass sentence is touched here.

### fix(security): the request boundary closes its leftovers — the probe's argv, zone ids, the plan builders, the dashboards selection (C22) (2026-10-12)

C22 says every request field that becomes a path component, an inventory key or an argv word is a closed charset,
checked at the seam and again at the join. A second pass found the boundary still open, starting with the one place
that needs no credential: `GET /probe/{collection}` and `GET /classify/{collection}` are open routes, and the name
went onto `ansible-doc`'s command line unvalidated (an option-shaped word such as `--version` was accepted).
`probe.deep_probe` now validates the name itself, at the join, so no caller can forget. Others. The validators
were anchored patterns used with `match()`, which say yes to a value followed by one newline, and `hostname()`
deferred to the standard library, which accepts an IPv6 zone id with *any* text after the `%` — braces included, in
a value that becomes an inventory variable Ansible templates. A name such as `null`, `true` or `123` is inside the
charset and is not a string once it stands unquoted in a file: a class key of `null` made every reader of the fleet
list hold `None`. The telemetry, logging and backup plan builders tested `modules/<key>/module.yml` for existence
before anything had checked `key`. A telemetry `dashboards` selection was written into the class's module file by
string formatting, where one name with a newline rewrote the class's role and inventory group, one with no newline
rewrote the item itself — its board id and its `derived` mark — and either later named the file an internet
download is written to; a name another class already used with a different board id would have replaced that
class's curated dashboard. And a unit's version pin was checked against a list of forbidden operators and then
hand-formatted, inside quotes, into the collection lockfile, where a quote made that file unparseable. All are
closed. The validators are patterns that end in `\Z`, applied with `fullmatch()`; no zone id. A value that goes
into a hand-formatted YAML line must read back as the same string **where it is written**
(`kontroll/yamlscalar.plain_scalar`: after `key: `, as a list item, before a comment, inside a flow mapping when
the caller renders into one — and never with a line-break character: the writers' single-line rule knew the
newline and the carriage return, and split the file on the Unicode separators as well). That rule
holds for every onboard field that becomes a name — the class key, the inventory group, the secrets domain, the
backend — a dashboard name, a backup remote name and the `text` and `hostname` knobs, and again inside the scalar
writers and the fleet writer, which fail closed. A host name is not held to it (`123` is a host name): the
generators that hand-format a name they read back — the Prometheus targets, the backup schedules, the play
preview, the trust sidecar's keyring — quote one that is not a plain word (`yamlscalar.word`), so a group stored
as `null` no longer reaches Prometheus as an absent label or Semaphore as no limit, no name is refused at deploy
time (a word the YAML reader cannot even build, such as a host named like a date that does not exist, is quoted
too — the check answers every string and never raises), and no shipped generated file changes by a byte. The key
is refused as each builder's first statement. A
dashboards item is exactly `{gnet, name}`, the name one path component; a name is one file and a board one name
— not repeated, not already declared with another id, not a second name for a declared board, and not an
existing file that does not say it is that board (no class declares the hand-authored logs dashboard, so any
board id was accepted under its name and the next fetch would have written a download over it).
`fetch-dashboards.py` fails closed on a bad item, on one name with two ids, and — before anything is fetched —
on a target file that is not the board it fetches; a board it writes says which board it is. The pin is one
plain version word and a unit's digest sixty-four hex digits; `gen-requirements.py` refuses, also at the promote
gate, to render a collection whose name is not `namespace.name` (each half starting with a letter) or whose
version holds anything but version characters, and a unit's trust statement whose signature policy is outside
the closed set or whose digest is not one — the policy stands unquoted in the trust sidecar, where one with line
breaks added an entry for another collection. A literal host var from an `inline` credential field is one plain
word, and a credential field's name an identifier before it enters a lookup expression. The service readers that
enumerate the fleet (classify, the Fleet index, the audit, the discovery sweep) now skip a line that is not a
class key instead of refusing the whole request; the deploy-time generators still read the list as it is. The
API's audit writer flattened a tab and a newline but
not a carriage return, which its reader treats as the end of a line, so one byte in a field that is audited before
it is validated cut the row short — everything the route recorded after that field (on a promote: the method, the
token prefix, the push flag) vanished from the audit query; both writers now share one flattener
(`kontroll/auditfmt.py`). A refusal is mapped once per app — the API and the GUI both answer **422** with a `code`
derived from the field (the GUI answered 500 on the reconfigure routes, and SECURITY.md said 400) — its message
names the field and the rule and no longer quotes the value, and a privileged refusal leaves an audit line naming
the field (`onboard-refuse` on the onboard routes, `request-refuse` with the route elsewhere). Fixed on the way: a
host onboarded under a dotted name (`sw.lab.example`) got a credential lookup that could never resolve; it now
reads one key, and every existing drop-in is unchanged. **For API clients:** a malformed class key on the
capability routes is 422 (was 500 or 404); a dashboards selection outside its shape or its bounds is 422 (any
other key, an uppercase or keyword name, a non-integer or out-of-range id, a repeated name, a name already
declared with another id or already a file of another board, a second name for a declared board); an inventory
group, secrets domain or backend that YAML reads as a keyword, a number or a date is refused at onboard; an
address with a zone id is refused at onboard and by the `ipv6` / `ip` knob types; the `hostname` knob — the
instance domain — refuses a keyword, a number or a date with a 422 (it used to be stored: `domain: null` is no
domain at all); a unit version that is not one plain version word
(letters, digits, `.`, `+`, `-`, starting with a letter or digit) is refused; a boundary or planner-guard refusal
on the onboard route gains a `code`; the refusal message no longer contains the value. The CLI prints a refused
argument as one line. **For an existing instance:** a class key, group, secrets domain or backend spelled like a
YAML keyword, or made only of digits, can no longer be onboarded — one that already exists keeps working, the
generators quote it; a dashboard file that does not carry the `gnetId` of the board declared under its name
stops `fetch-dashboards.py`, with the file named, until one of the two is renamed or the file is deleted; and
`gen-requirements.py` — at bootstrap, at deploy-stack and at the promote gate — now stops on an ACTIVE unit whose
`signature` is not one of the policies or whose `sha256` is not sixty-four lowercase hex digits, and on a
collection name with a half that does not start with a letter. It used to render those as they stood.
`python3 scripts/gen-actuation.py --check` (offline) shows such a unit beforehand: run it on a node before its
next deploy.
Proofs: `tests/unit/test_request_boundary.py`, `test_yamlscalar.py` (every position a writer uses, exhaustively
for short strings), `test_validate.py`, `test_blockwrite.py`, `test_observe.py`, `test_fetch_dashboards.py`,
`test_gen_observability.py`, `test_gen_backup.py`, `test_actuation_service.py`, `test_settings.py`,
`test_identity.py`, `test_onboard_ssh_key.py`, `test_create_unit_service.py`, `test_actuation_registry.py`,
`test_api_audit.py` and the route pins in `tests/integration/` — each new refusal test fails on the previous
tree; the accept tables, the read-back tests and the dashboards write path are new coverage. SECURITY.md C22.

### docs(process): which review loop a change gets, and a home for decisions (2026-10-12)

The process documents had two sentences on when to run a multi-agent review ("on a wave… for a one-off, fix it
inline") and no notion of a security boundary — while the reviews themselves had grown to several times the
size of the code they reviewed, and the faults that reached a live box were found by running, after large
as-built reviews had passed. `docs/agent-workflow.md` now says which loop a change gets. **The full loop** (the
blackboard runner, then build, then run it, before any further review) is for a security-boundary change: one
that adds a route, a request field, an outbound client, a container privilege, a secret domain or a recipient
rule; changes what reaches `main` without a human; or needs a `SECURITY.md` claim changed to stay true.
**Everything else gets the light loop**: the pull request is the note, the change is built with its tests and
run first, and a diff that touches a boundary file gets one independent read. A table of the files where a
boundary lives is a prompt, not a test — a change to one says in a line why the tests do not apply — and a check
keeps every path in it real. Decisions get a home: `docs/adr/` holds the record template, the rule that a record
is superseded and never edited, and an index that already lists the decisions the 2026-10 review left open.
`docs/reviews/README.md` gains the index of published runs, and no longer says that post-release dossiers "are
published" there (none has been). The runner (`.claude/workflows/blackboard.js`) returns the texts the main
thread writes after a run — the synthesis's opening lines, a decision-record stub, the index row — takes an
`effort` per phase and per agent, and logs one line when a run of several agents names no boundary; its header
no longer contradicts its README about how it is invoked. The read-only contract it prepends to every agent is
untouched, and pinned byte for byte. The pull-request template asks which loop. Nothing at runtime changes.
Proofs: `tests/unit/test_review_loop.py`.

### test(gates): the hygiene sweeps read every role, and the read-only pin follows what a view can reach (2026-10-12)

Two guards covered less than their names said. The `set_fact` and self-referencing-variable sweeps read
`ansible/playbooks/` only, although both faults are fatal on a real run and the role files are what every device
play executes; and `assert_read_only` — the pin behind "a read view never gains an actuation path" — read the
pinned function's **direct** calls, so a view whose write sat one helper away passed, and a refactor that split a
view into helpers un-pinned it without anyone deciding so. The sweeps now walk every role's task and handler files
at any depth, the copy-me `.role-template` included, and the variable sweep also reads role `defaults/` and
`vars/` and the playbook-adjacent `group_vars`; a role YAML file in a place neither sweep reads fails a test
instead of escaping. The pin now follows, by name, the helpers of the same module (called, or handed on as a
callback) and the functions of other repository modules reached through an import, and in each looks for a
registered write verb **and** for the write primitives themselves — so a pinned function that writes with
`Path.write_text` or `os.replace` is refused even where the completeness gate would only have asked for it to be
registered. A failure names the path, e.g. `fleet_provenance -> paths.write_root -> (a direct write)`. Both
guards are clean on today's tree — every pinned read view, at the strictest closure — and each is proven by a
planted violation. What the pin cannot follow is dynamic dispatch (a computed `getattr`, a function passed in as
data). Proofs: `tests/unit/test_playbook_setfact_hygiene.py`, `tests/unit/test_playbook_vars_hygiene.py`,
`tests/unit/test_readonly_completeness.py`. No product code changes.

### fix(tests,generators): the suite and four product gates read the shipped example, never a private fleet (2026-10-12)

`kontroll.paths` resolves instance config overlay-first, so on a tree that carries a real `instance/` — the
maintainer's — the suite and four generators' `--check` (`gen-backup`, `gen-prometheus-jobs`, `gen-exporters`,
`gen-secret-env`) compared committed, example-derived files with a private fleet. They were green only because
that fleet enabled the same classes as `instance.example/`; the first real fleet edit would have turned named
tests and the `gen-backup` gate red on that tree alone, with a "stale" committed file nobody could regenerate from
a public clone. The four generators now do what the other four already did: `--check` validates against
`instance.example/`, the new `--example` regenerates from it, and the bare run — what a deploy calls on a node —
still reads that node's overlay (`gen-exporters` reads the example's `device_trust` under `--check` too, instead of
a hard-coded `instance/instance.yml`). For the suite, the resolver's two overlay probes became functions and
`tests/conftest.py` wraps them at import time so that, at the real checkout only, they answer "absent"
(`tests/_hermetic.py`); a tmp root is untouched, and there is no production switch. The file probe also confines
its own join now: the path is normalised and one that climbs out of the write root with `..` is "absent" (a
symlinked `instance/` is still followed), where it used to ask the filesystem about wherever the joined string
pointed. **On a deployed node** whose
fleet differs from the example, `tests/validate.sh` now reports these four files stale after a deploy rewrote
them — as it already did for the other four; that is the node's own state, not a fault. Proofs:
`tests/unit/test_suite_is_overlay_hermetic.py`, `tests/unit/test_generators_check_reads_the_example.py`,
`tests/unit/test_paths_overlay.py`.

### fix(collections): `community.docker` is a core collection — `deploy-stack` no longer depends on a device class (2026-10-12)

`deploy-stack` builds the control image and its networks with `community.docker` on every fleet, but the only
declaration of that collection was the `docker_host` DEVICE class. On a host-direct node whose fleet has no Docker
hosts the bootstrap installed no `community.docker` and `deploy-stack` could not resolve its first image build. It
was never seen because the compose-native path hid it: the installer image wrote a fabricated
`enabled_modules: [docker_host]` fleet into its own tree, generated from it, and deleted it again. `modules/_core.yml`
now carries the collection (same floor as the class, which keeps its own declaration — a class describes itself),
`gen-requirements.py --core` resolves that file alone without reading any fleet, and the installer image uses it.
**On an existing node:** the generated lockfile and trust sidecar change (one more entry, or the same entries in a
different order), so `gen-requirements.py --check` reports them STALE until the next `bootstrap` or local-build
`deploy-stack` regenerates them — a true report that heals itself. Where the collection is already installed the
control-node bootstrap fetches nothing. A box that builds its runner image locally rebuilds that image's collection
layer once on its next `deploy-stack` (the lockfile the image copies in is byte-different, so the layer cache
misses) — that build needs Galaxy and PyPI, as a fleet change does. No image needs republishing: the published
images already contain the collection.

### chore(tests): the Molecule scaffold is removed — it could not fail (2026-10-12)

`tests/molecule/default` converged one role with `molecule_test: true`, and every role's `check` and `backup`
entrypoint answered that flag by skipping its device tasks — so the converge ran nothing and its idempotence step
could not fail. No gate ran it, yet the documents listed it as the L2/L4 role test. The scenario, its two lint
exclusions and the `molecule_test` conditional in every role (and in the role template a new class is copied from)
are gone; the flag was never set outside the deleted converge, so no run changes. The test documents now say what
is true: playbooks are parsed against the mock inventory, nothing evaluates a role yet, and a check-mode run of the
plays in CI is the open item. `tests/unit/test_roles_have_no_test_only_branch.py` keeps the branch from coming back
with the next role copied from an old checkout.

### fix(gui): only literal markup reaches `innerHTML` — the paint rule becomes an allow-list (C19) (2026-10-12)

The Pending panel painted a staged proposal's run id, commit subject and changed path names through `innerHTML`.
Those bytes are whatever the proposal's author committed, a run id is a git ref name (which may hold `<`, `>` and
`=`) that nothing validates before it is listed, and none of it has been promoted — so markup there ran in the
operator's authenticated session on opening the list. It was one of fifty-nine sinks that painted a non-literal
value: the error line of every panel not yet converted (a direct `.innerHTML =`), the dialog titles, the fleet,
service, settings and homepage rows, the capability dialog's whole body (method names in `<option value>`, a
server-supplied `data-testid`), the keygen plan — and the freshly minted private key itself. The C19 gate saw none of
them: it was a denylist of seven substrings over `E()` arguments and never read a direct assignment. Every one of
those sites now builds child elements or fills an empty element's `textContent`, and the gate is an allow-list (an
`E()` markup argument and every `.innerHTML` right-hand side must be literal-only, and the other spellings of the
sink that a token scan can read are refused), so the next value is covered whatever it is called. Closed with it,
because text-painting does not cover them: a link's `href` is assigned in one helper, `LINK()`, and only when the
browser's own URL parser resolves the value to http(s) (`safeHref()`); and the request paths that carried an
unencoded segment
(`/api/secrets/<domain>`, `/api/keygen/<role>`, `/api/capability/<cap>`) are `encodeURIComponent`-ed — unencoded, a
secret domain of `../keygen/control` made the secret dialog's Save reach the route that mints a key. Two visible
changes, both corrections: a legitimate value containing `<` is shown in full (the keygen next-step hint shows its
`<run_id>` placeholder, which the browser used to swallow as an unknown tag), and a link target that is not a web
link is shown as text instead of being linked. Proofs: `tests/unit/test_card_paint_gate.py` (the allow-list, each
bypass shape its reviews found planted into the real template, the link, attribute, tag-name and request-path
rules) and `tests/e2e/test_paint_rule.py` (a browser, panel by panel: the payload is text, no element, nothing
runs — each test was run against the previous template and failed). New testids `pending-head`,
`pending-subject`, `service-link`, `cap-link`. SECURITY.md C19.

### docs(images): the published packages are public — R-IMG-1 retired (2026-10-11)

`ghcr.io/netcanon/kontroll-{control,vector,installer}` were made public on 2026-10-11 (the one-way package setting,
done by hand — there is no API). Verified from a credential-less registry token: `v0.1.2` resolves by tag and by
digest to exactly what `docker/images.lock.yml` pins. The control node therefore holds no registry credential under
either deploy path, and the accepted risk R-IMG-1 is closed in SECURITY.md (the strikethrough form the Vector socket
row set). Every "`docker login ghcr.io` first" instruction now says the pull is anonymous and a login applies only
to a private package: `docs/SETUP.md`, `docker/README.md`, `docs/public-split.md` (table, sync recipe, checklist
step 5), the `publish-images.yml` header, the deploy play's comments and fail-closed message,
`gen-image-digests.py`, the `scripts/kontroll.sh` pre-pull rationale. One correction on the way: a new package does
NOT inherit the repository's visibility — the first publish from this public repository (2026-10-09) produced
private packages; a package starts private until its owner flips it. No behaviour change.

### fix(gui): a boundary refusal answers with a per-field `code` the browser can act on (2026-10-10)

The GUI relay's 422 for a request the C22 boundary refuses carried only the message; the browser renders
next-steps copy per refusal `code` (the permissive edition keys its install-offer copy on `invalid_collection`),
so a malformed collection name lost its guidance after #24. The code is now derived from the refused FIELD —
`collection` → `invalid_collection`, the device-class key → `invalid_device_class_key` — never from the value.
Pinned in `tests/integration/test_gui_api.py`.

### fix(api,gui): a request the boundary refuses leaves an audit line, by field name (2026-10-10)

#24's closed-charset validators refused before any write but through the generic `ValueError` path, which neither
the API route nor the GUI audited — a refused onboard left no trace, the blind spot the tine's own refusal test
caught at the catch-up merge. The validators now raise `paths.RequestBoundaryError` (still a ValueError, so the
422/400 mapping is unchanged) carrying the FIELD, and both routes write `onboard-refuse reason=request-boundary
field=<name>` — the name, never the value: a refused `key` of `../../etc` must not be copied into the audit log
either. Pinned on the API (the audit line exists and does not contain the value) and in
`tests/unit/test_request_boundary.py`.

### fix(promote): the gate's whitelisted environment lets Python's own path configuration through (2026-10-10)

#23 ran the FIX-M9 generator under a whitelisted environment; it dropped `PYTHONPATH`/`PYTHONUSERBASE`/`VIRTUAL_ENV`
too, so on a runner (or a venv-less box) that locates the interpreter's packages that way the child could not
`import yaml` and the gate reported every proposal as a conflict. The self-hosted CI found it on the first private
sync. The five Python path variables now pass through — paths, not secrets; `SOPS_AGE_KEY` and `KONTROLL_*` are
still withheld, and the test asserts both halves.

### chore(docker): `images.lock.yml` pinned to the v0.1.2 images (2026-10-10)

`v0.1.2` is the public line's first release tag: the un-stranded fixes (#14–#17), the Phase 2 safety set
(#18–#26) and the gate PR (#13). `publish-images.yml` built the three images from the tag and the lock now names
`ghcr.io/netcanon/kontroll-{control,vector,installer}:v0.1.2` by digest (`gen-image-digests.py --refresh v0.1.2
--owner netcanon`). Each image's `org.opencontainers.image.revision` label names the tagged commit. The packages
stay private until the owner makes them public by hand; a `use_published_images=true` deploy needs a
`read:packages` login until then (SECURITY.md R-IMG-1).

### fix(validate-live): the TLS floor is stated, not assumed (2026-10-10)

`gen-validate-live.py` built its TLS contexts with `ssl.create_default_context()` and relied on the interpreter's
default protocol floor (CodeQL `py/insecure-protocol`; the 2026-10-08 review's one-liner). One constructor,
`LiveFetchers._tls_context`, now builds every outbound context — verifying or, for the self-signed classes,
unverified — with `minimum_version = TLSv1_2` set explicitly, so an interpreter build or an `OPENSSL_CONF` cannot
lower it and nobody has to know the default to know the floor. Two tests: the floor on both shapes, and a source
pin that no other line in the script builds a context.

### fix(install): the mirror check reads with `ls-remote` and prescribes a rebase (2026-10-10)

The canonical mirror refusal (public #14) was ported from the tine's first cut, which a live run had already
corrected on 2026-07-28: its `git fetch local` ran as root inside the installer container and left a root-owned
`.git/FETCH_HEAD` in the operator's worktree, so the operator's own `git fetch` — the first half of the remedy the
message prints — died with "Permission denied"; and the remedy said `merge --ff-only local/main`, which cannot
work because this play commits an instance-state commit of its own every run (the histories diverge rather than
lag). Now the canonical's `main` sha is READ with `git ls-remote` (writes nothing anywhere), the ancestry check runs
on that sha and refuses on ANY non-zero (rc 128, objects never fetched, is as unproven as rc 1), and the remedy is
`git rebase local/main` — a plain fast-forward when there is nothing to replay. `tests/unit/test_local_canonical.py`
pins the read, the remedy, the fail-closed condition and that no task fetches into the worktree.


### fix(service): every request field that becomes a path or an inventory key is a closed charset, and repository writes are confined (2026-10-09)

The onboard planner validated only the collection's installed-ness; `key`, `group`, `host`, `host_name` and
`secrets` were bare strings joined into `modules/<key>/`, `onboarded-<key>.yml` and
`ansible/secrets/<domain>.sops.yml` and rendered into YAML, and the Fleet index painted host names through
`innerHTML` (the 2026-10-08 review, finding 1; CodeQL's 45 `py/path-injection` alerts are this one class of
join). New `kontroll.paths` validators — `component()`, `collection_fqcn()`, `hostname()` — run at the service
seam (`build_onboard_plan`, before anything is read, probed or written) and again at every join (`module_file`,
the drop-in inventory path, the secret-domain path, the actuation unit path, the probe's collection-dir walk);
`paths.confined()` resolves every drop-in write under `write_root()` and refuses anything that climbs out. The
API answers **422**, the GUI **400**, without echoing the value. The Fleet index paints host names and
addresses as text. New SECURITY.md **C22**; `tests/unit/test_request_boundary.py` plus a parametrised API pin
that git is never touched on a refused request.


### fix(promote): the pin-conflict gate runs trusted code over the proposal's data, never the proposal's code (2026-10-09)

FIX-M9 extracted `proposed/<run_id>` and executed ITS `scripts/gen-requirements.py` — unreviewed code from the very
ref being judged, as root on the `sudo` CLI and with `SOPS_AGE_KEY` in scope on the Semaphore path, with the
caller's whole environment minus one variable (the 2026-10-08 review, finding 3). The gate now runs THIS tree's
generator with a new `--root <extract>` data-root option (modules/, fleet and actuation read from the extract; the
code stays the caller's) from the trusted root, under a whitelisted minimal environment. The test plants a
proposal whose own generator is a canary that reports "no conflict" while its data conflicts: the gate refuses
and the canary never runs (`tests/unit/test_kontroll_promote.py`).

### fix(ansible): the canonical's hooks are root-owned and outside the gid-1001 write-grant (2026-10-09)

`local-canonical.yml` installed the C10 update hook and then `chown -R <operator>:1001` + `chmod -R g+w` the whole
bare repository on every deploy (its `find ! -perm -020` detector always matched the fresh hook), leaving `hooks/`
group-writable by the uid-1001 service that holds the canonical `:rw` under `api_privileged`. A dropped
`reference-transaction` or `update` hook would have run as root on the next `sudo kontroll-promote` (the
2026-10-08 review, finding 2). The grants are now detect-then-fix `find` commands that prune `hooks/`, and a
final task keeps `hooks/` root:root 0755 — every git actor needs only to read and execute hooks. A second run
reports 0 changed. `tests/unit/test_local_canonical.py` pins the prune on every grant, the root re-own, and its
order after the grants; docs/install-from-scratch.md, docs/privileged-mutation-enablement.md and SECURITY.md C10
say so.


### fix(docker): the socket proxy moves to its own internal network; Vector is its only peer (2026-10-09)

`docker-socket-proxy` is GET/HEAD-only, but `GET /containers/<id>/json` returns a container's `Config.Env` — every
secret the stack passes by environment, `SEMAPHORE_ACCESS_KEY_ENCRYPTION` among them — and it sat on the shared
`kontroll` network that every service joins, so any container could have asked (the 2026-10-08 review, finding
4). The proxy now joins only a new `kontroll-socket` network, created `internal` by deploy-stack; Vector joins it
as the sole consumer and still reaches the proxy by service name. `tests/unit/test_compose_logging.py` pins the
proxy's networks, Vector's membership, that no other fragment joins the private network, and the internal
creation. SECURITY.md C12.

### fix(docker): Homepage gets its own env file and never reads the stack `.env` again (2026-10-09)

`homepage.yaml` `env_file`d the whole stack `.env`: the Semaphore DB password and access-key encryption secret,
the Grafana admin password, the GUI password, the API token and every exporter credential, handed to an
unauthenticated portal that needs none of them (the 2026-10-08 review, finding 5). deploy-stack now renders
`docker/.env.homepage` with exactly the `homepage_var_*` keys of the `dashboards` SOPS domain (upper-cased to the
`HOMEPAGE_VAR_*` names a tile's `{{HOMEPAGE_VAR_X}}` expects) and the fragment reads that file alone. Adding a
widget token is one key in the domain. `docker/.env.example` points there instead of listing the tokens;
`tests/unit/test_homepage_env_isolation.py` pins that no fragment `env_file`s the stack env, that the render task
emits only widget keys under `no_log`, and that the new file is git-ignored.

### fix(ansible): the reload play no longer defines a variable in terms of itself (2026-10-09)

`reload-observability.yml` declared the play var `prometheus_url: "{{ prometheus_url | default(…) }}"`, a
recursive template that ansible-core 2.19's engine refuses ("recursive loop detected"), so the only human reload
verb would have failed before its first task on a current control node (the 2026-10-08 review, finding 11). The
play now reads the operator override through a private name; `-e prometheus_url=…` works as before.
`tests/unit/test_playbook_vars_hygiene.py` sweeps every playbook's play-level and task-level `vars:` for a key
whose value names itself, with a planted-shape proof that the detector fires, so the next one fails in CI rather
than on a box. (`set_fact` self-defaults are deliberately out of scope: they template against the pre-task
context, which is what makes them work.)


### fix(gitio): the promote is a compare-and-swap, never a check-then-set (2026-10-09)

`promote_ref` ran `merge-base --is-ancestor` and then `update-ref refs/heads/main <proposal>` with no old-value
operand (the 2026-10-08 review, finding 9). Two promotes racing — the Semaphore task and a CLI run, or two CLI
runs — let the second overwrite the first with a ref that is no longer a fast-forward of the `main` it replaced:
a promote lost silently while both reported success. The promote now reads `main`'s object id, checks the
fast-forward against it and passes it to `update-ref` as the old value, so git refuses the loser ("is at X but
expected Y") and says so. `tests/unit/test_promote_cas.py` races two real proposals in a real bare repository and
proves the loser is refused and the winner kept.


### fix(deploy): the mandated dry-run works on a brand-new node, and a credential-less SNMP exporter no longer kills the stack (2026-10-09)

Ported from the private instance's tine, where a from-scratch rebuild found them on 2026-07-28 (the 2026-10-08
review, finding 8):
- `--check` skips the tasks that CREATE things, so on a node where nothing had ever been applied the tasks that
  clone from the canonical or take ownership of a TLS cert failed outright, and the mandated `--check --diff` could
  not pass on day one. One narrow fact, `_fresh_preview` (check mode AND no canonical yet), gates the sites that
  provably cannot be previewed, and a PARTIAL PREVIEW banner says what was skipped. A provisioned box keeps full
  fidelity, and the G9 age-key refusal is deliberately NOT gated: a fresh node is exactly where that precondition
  has never been met (`tests/unit/test_fresh_node_preview.py`).
- That G9 refusal now says what to do: it fails when the onboard-gui age key is missing OR unreadable by the
  runtime uid, and prints the exact `install` command (docs/SETUP.md carries the same);
  `tests/check-storage-chown.py` recognises the stat-then-assert shape as a fail-closed guard
  (`tests/unit/test_g9_age_key_refusal.py`).
- `snmp.yml` is a FILE bind into snmp-exporter but was rendered only when SNMPv3 credentials existed, so on a
  fresh node Docker auto-created the bind source as a root-owned DIRECTORY and `compose up` died. It is now always
  rendered (the template omits `auths:` without a domain; 0600 + gitignored unchanged) and snmp-exporter is
  dropped from the up-list until credentials exist, with a message naming the GUI dialog that adds them — one
  fact for both decisions. The up-list's intermediate is a task `vars:` entry, not a sibling `set_fact` key (those
  template against the pre-task context and die undefined at render, past `--syntax-check` and ansible-lint);
  `tests/unit/test_playbook_setfact_hygiene.py` sweeps every playbook for that shape and
  `tests/unit/test_snmp_config_render.py` evaluates the real Jinja.


### fix(fleet): the edge check has teeth, a fresh box can back up, and two SECURITY.md claims are true again (2026-10-09)

Ported from the private instance's tine (fixed there 2026-07-29; main never got it — the 2026-10-08 review,
finding 8):
- `roles/backend_api/tasks/check.yml` registered its `uri` result with `failed_when: false` and never read it, so
  a 200, a 401, a 403 and a refused connection were indistinguishable: a green check against a device it had never
  authenticated to. It now branches on the status, separates "credential rejected" from "device never answered",
  resolves the token under BOTH names the repo ever used (the recipe's `token_var` and the onboarded
  `<host>_api_token`) and refuses an unresolvable or empty token by NAME, never value
  (`tests/unit/test_backend_api_check_teeth.py`).
- `backup-configs.yml` referenced `config_backup_dir` fifteen times but the scaffold shipped no `group_vars`, so no
  instance created by `kontroll-init --fresh` could run it. `instance.example/inventory/group_vars/all.yml` now
  ships the default (`tests/unit/test_capture_dir_scaffolded.py`).
- SECURITY.md C1 claimed every secret is encrypted to two age recipients and that key loss is recoverable. Every
  `key_groups` in the shipped `.sops.yaml` names ONE recipient and `--fresh` mints the same shape: break-glass is
  NOT IMPLEMENTED and losing the control key is catastrophic. Recorded as **R-KEY-1**; the covering test is
  bidirectional (`tests/unit/test_break_glass_claim_is_honest.py` reads the template's recipient count and requires
  the doc to match either way).
- A functional group must not pin a vendor: `tests/unit/test_group_vars_are_vendor_neutral.py` fails any
  `group_vars` file (the scaffold's or an instance's committed overlay) that sets `ansible_network_os` or a vendor
  connection plugin — such a group is a second, invisible dispatch seam competing with `device_role`. The scaffold
  gains an `inventory/README.md` that teaches the rule where the old private README taught the violation.


### fix(gui): the GUI renders foreign bytes as text, never markup (C19) (2026-10-09)

Ported from the private instance's tine, where it has been live since July (2026-10-08 review, finding 8). The
search panel painted a collection's Galaxy `description`, name, version and origin through `innerHTML`, so a
published collection whose description is `<img src=x onerror=…>` ran script in the operator's authenticated
session on search alone, with no click and no onboard. Every value kontroll did not author now reaches the DOM as
`textContent` (the card's meta suffix is a child element, because the interpolation was the sink), and the same
gate closed eleven more sinks of the class: the backup-viewer error and unavailable paths, the settings error
line, the services description, the secret-domain and keygen-role pickers. A CSP (`object-src`, `base-uri`,
`frame-ancestors`, `nosniff`) ships as the second layer; `script-src` is deliberately absent while `index.html`
carries its one inline script. Proofs: `tests/unit/test_card_paint_gate.py` scans every `E()` call site for a
foreign field and fails on planted sinks; the e2e serves a record whose description IS the payload and asserts
zero elements are created. SECURITY.md C19.

### fix(docker,install): a fresh install comes up, and hands its files to the operator (2026-10-09)

Three defects that were fixed on the private instance's permissive tine in July and never reached `main`
(the 2026-10-08 review, finding 8), ported by defect locus:
- `docker-socket-proxy` ran with `read_only: true`, but the pinned `tecnativa/docker-socket-proxy:0.3.0` renders
  its shipped `haproxy.cfg.template` into its own root fs at start, so every fresh deploy crash-looped the proxy
  (and with it Vector's `docker_logs`). Now `read_only: false`; the real controls (`POST=0` GET/HEAD-only filter,
  `no-new-privileges`, portless, socket `:ro`) are unchanged (SECURITY.md C12).
  `tests/unit/test_service_writable_root.py` pins the image-to-writable-root requirement as a fail-closed deny table.
- `fresh-init` ran as root in the installer container and handed back a root-owned `instance/` and age key, the
  very files the next step tells the operator to edit. The verb is no longer `exec`'d: it chowns the scaffold,
  the key and its 0700 directory to the invoking operator (`KONTROLL_OPERATOR_UID`, MF-1) and keeps the
  scaffold's own exit code (`test_installer_phase1.py`).
- `local-canonical.yml`'s mirror push was rejected with git's generic "fetch first" hint whenever a promote had
  advanced the canonical past the worktree, which is normal under propose-then-promote and unreadable on a
  control plane. It now fetches, checks ancestry and refuses with the exact `merge --ff-only` to run; never
  `--force`, because the canonical is where promotes land (`test_local_canonical.py`).


### chore(ci,tests): the permissive tine is gated on push, and Windows runs the one gate (2026-10-09)

Two drifts the 2026-10-08 full review found (its finding 18 and ruling N18). The `homelab*` branches now trigger
the four gating workflows (`ci`, `pii-guard`, `security`, `zizmor`) on push, exactly like `main`: the private
instance repository's permissive tine had only ever been gated through a never-merge pull request against
`main`, one mis-click from landing it on a repository with no branch protection. The entry is inert on a public
repository, which has no such branch, and `publish-images.yml` still runs on `v*` tags only. And
`tests/validate.ps1` is now a shim over `tests/validate.sh` through Git for Windows' bash: the Windows runner had
drifted to 12 of the gate's 30+ steps (no `gen-*.py --check` staleness gate, no identifier-leak gate), so a
contributor could pass locally and fail CI. Measured on a Windows workstation the shim runs 29 steps and SKIPs 7
for Linux-only tools; `--strict` keeps its CI meaning. Both pinned in `tests/unit/test_ci_workflows.py`.


### chore(docker): `images.lock.yml` re-pinned to the public owner's packages (follow-up F10) (2026-10-09)

The lock pinned `ghcr.io/netcanon-dev/kontroll-*` at `v0.1.1` — private packages whose baked tree predates the
identifier scrub. The three images were re-published from the public `main` (`97fb671`) by `publish-images.yml`
under the repository's owner with the tag `sha-97fb671`, and the lock now names
`ghcr.io/netcanon/kontroll-{control,vector,installer}` at that tag by digest (one command:
`gen-image-digests.py --refresh sha-97fb671 --owner netcanon`, fail-closed). Each image's
`org.opencontainers.image.revision` label names that commit and its `source` label this repository. The packages
stay private until the owner makes them public by hand (a package setting the flip does not touch); the default local build
is unaffected. The lock writer now writes
LF on every platform.

### fix(repo): the overlay is re-included by `instance/.gitignore`, and the leak guard checks the overlay invariant (2026-10-09)

The public cut ignored `instance/` outright. Three consequences, found by the as-built review
(`docs/reviews/2026-10-09-public-split-follow-ups/`, report 12): an install made from the public tree committed
**no overlay** into the control node's canonical (`local-canonical.yml` runs `git add -A`, which skips ignored
paths), so Semaphore's clone silently read the shipped example tier and a GUI onboard that created an overlay
file failed at `git add`; the PII guard's "overlay never tracked" probe used `git check-ignore` without
`--no-index`, which never reports a TRACKED path, so a `git add -f` passed exactly the case it existed for; and
`docs/reviews/` was never scanned on any tree although its README said it was. Now:
- the root `.gitignore` ignores the overlay's **entries** (`/instance/*`, `!/instance/.gitignore`) and the shipped
  `instance.example/.gitignore` — scaffolded by `kontroll-init --fresh` as `instance/.gitignore` — re-includes the
  overlay's known entries (`.sops.yaml`, `instance.yml`, `fleet.yml`, `leak-tokens.txt`, `inventory/`, `secrets/`,
  `dashboards/`, `trust/`), while the root secret patterns (`*.agekey`, `keys.txt`, `*.dec`,
  `trust/observed-digests.yml`, `trust/*.gpg`) still apply inside. A public tree has no such file and ignores
  everything; an instance repository and the canonical track the overlay; the root `.gitignore` is **identical in
  both repositories**, so it no longer conflicts on a sync;
- `tests/_leak_guard.py --tree` (all three seats) checks the **overlay invariant** — PUBLIC (nothing tracked,
  entries ignored) or INSTANCE (tracked, every tracked path re-included) — with `check-ignore --no-index`, and
  reports a `git add -f`, a lost root rule or a dropped re-include file as `overlay` findings; the workflow's
  separate probe step is gone;
- `docs/reviews/` is stripped only on an instance tree; a public tree scans its dossiers like any other file.
Proven with real git in `tests/unit/test_leak_guard.py` (the shipped rules on a scratch repository: what `git add -A`
stages in each state, the three failure modes, `--tree`'s exit and summary) and `test_kontroll_init.py` (the
scaffold writes the re-include file).

### chore(ci,docs): public-split follow-ups — owner-agnostic image publish, the zizmor SARIF seat, Dependabot floors, two cosmetic fixes (2026-10-09)

Review of record: `docs/reviews/2026-10-09-public-split-follow-ups/` (three read-only agents over this change, the
trailer seam and the private graft; the must-fixes are folded in here).
- **`publish-images.yml` publishes under the repository's owner** (`ghcr.io/<owner>/kontroll-*`, lowercased from
  `github.repository_owner`) instead of a hard-coded private org, so the same file publishes the public tool's
  packages and any instance's own. A dispatch is honoured from `main` or a `v*` tag only, the tag must fit the
  docker tag grammar, a `sha-<short>` tag must name the commit being built, and every image carries
  `org.opencontainers.image.source`/`revision`. The four `docker/*` actions in that job are now SHA-pinned (the
  repo's own `.github/zizmor.yml` policy, which the comment had claimed was moot and SECURITY.md C6 had called
  "forced"; both corrected). `gen-image-digests.py --refresh` **fails closed** (nothing written, exit 1, when a
  digest does not resolve — it used to keep the old digest under the new ref/tag) and takes `--owner <owner>` so the
  re-pin is one command. The lock itself is re-pinned in a separate change once the public packages exist and are
  pullable (`docs/public-split.md`, "Publishing the images"); until then it still names the private org's
  `v0.1.1` images. The synthetic refs in three test fixtures no longer name an org.
- **zizmor gains a SARIF seat** as its own job, gated on the repository variable `KONTROLL_CODE_SCANNING=true`
  (set by the flip on the public repo, where code scanning is free; the private instance repo leaves it unset and
  the job — and its `security-events: write` grant — never exists there). The scan is rooted at the repository so
  every uploaded URI is real, nothing masks a tool failure, and both jobs stay advisory.
- **Dependabot stops chasing `>=` floors**: the pip ecosystems use `versioning-strategy: increase-if-necessary`,
  which for pure floors means no version PRs at all (five floor-bump PRs arrived the first week); floors are
  raised by hand when a release matters. `api/requirements.txt` is now covered by Dependabot and `pip-audit`
  audits the gui, api and tests files (the api file had been invisible to both).
- `docs/public-split.md` gains the weekly sync recipe with its acceptance checks and conflict rules (merge commits
  only; tool fixes land public-first; the `.gitignore` resolution; token parity; Dependabot twins; tags; the image
  lock and the control node's registry credential), the image publish + re-pin order, and the package-visibility
  step in the flip checklist; `THIRD-PARTY-NOTICES.md` says what the published images redistribute.
- Four text-pin tests (`tests/unit/test_ci_workflows.py`) and two `--refresh` tests keep all of the above true.
- Cosmetic: the generated Loki overrides header pointed at a private review-dossier path (now
  `docs/logging-architecture.md`; `docker/loki/overrides/retention.generated.yaml` regenerated);
  `configure-semaphore.py` dropped a defined-and-unused `REPO_GIT_URL` naming a remote the tool does not own;
  leftover "private ghcr" wording swept.
### fix(gitio): one configurable attribution trailer on machine-made commits (public-split follow-up F9) (2026-10-09)

Eighteen call sites — the API routes, the onboard-GUI and `galaxy.py --commit` — each carried a literal
`Co-Authored-By: <the model that wrote the code>` trailer, stamped into every commit the control plane makes on
an operator's behalf: the wrong attribution on every proposal, eighteen places to edit, and a model name baked
into a public tool's runtime output (review `docs/reviews/2026-10-08-public-split-pii-sweep/`, F9). Now ONE seam:
`gitio.commit_message_args()` builds every `git commit -m` list and appends the single trailer, last and never
duplicated; `commit_and_push` (the API/GUI write seam) and both inline `galaxy.py` commits go through it, and no
runtime file under `api/`, `gui/` or `scripts/` may carry the literal (grep-gate in
`tests/unit/test_commit_trailer.py`). The trailer is a deployment setting: `KONTROLL_COMMIT_TRAILER`
(`docker/.env.example`; passed through `api.yaml`/`onboard-gui.yaml`; rendered by `deploy-stack.yml` from
`kontroll_commit_trailer`; registered in `gen-secret-env`'s platform-core set so no descriptor can shadow it;
the operator CLI reads the same variable from its shell) — empty keeps the default
`Co-Authored-By: kontroll <kontroll@localhost>` (the identity the containers commit as), `none`/`off`/`disabled`
turn it off, any other text is used verbatim. Every commit path already carried a trailer: only its text changes
and it becomes a setting. A second gate walks the AST so no `["git", "commit", …]` argv outside gitio can bypass
the seam. Baked-code manifest regenerated.

### fix(repo): restore the executable bit on every shebang'd entry point, with a guard (2026-10-09)

The public cut (`git archive | tar -x` on a Windows host) dropped the executable bit on all 48 shebang'd files —
`./bootstrap.sh`, `scripts/kontroll.sh`, the installer entrypoint, the bundle/launch-kit scripts — so a Linux
clone failed `docs/SETUP.md`'s first command with "Permission denied" while every CI gate stayed green (CI runs
them via `bash`/`python3`); the private tree had been inconsistent too (11 of 48). Every tracked file outside
`docs/` whose first line is a shebang is now mode 100755 and nothing else is, pinned by
`tests/unit/test_executable_bits.py` (reads the git index mode, so it holds on a Windows checkout).

### feat(repo): the public split — identifier-free shippable tree, a standing leak guard, GitHub-hosted-or-self-hosted CI (genericization Phase 5) (2026-10-08)

Design-of-record: `docs/reviews/2026-10-08-public-split-pii-sweep/` (a 5-agent read-only sweep + a mechanical
scan of the tree, the full git history and every commit message); runbook `docs/public-split.md`; control
SECURITY.md **C21**. The tool goes public as a **fresh-history repository** cut from `main` without `instance/`
and the pre-split `docs/reviews/` (the same strip set as `make-bundle.sh`); this repository stays the private
instance repository. What this change makes true:
- **A checkout with no `instance/` passes its own gates.** `kontroll.paths.resolve()` gains a third, READ-only
  tier: on a tree with no `instance/` dir and no legacy stub it falls through to the shipped
  `instance.example/`, so the generators' `--check` gates, the service readers and the suite run against
  TEST-NET example data instead of dying on a missing fleet/inventory (60+ collection errors before). The tier
  is off once `instance/` exists (a configured node that lost a file still fails loud), never rewrites
  `overlay_rel()` (the staged/git-add path), and `resolve(write=True)` — passed by every writer (`keygen`'s
  `.sops.yaml`, `enable_in_fleet`, `kontroll-init`'s personalize) — disables it, so the example can never
  become a write target. Pinned by four new tests in `test_paths_overlay.py`.
- **One identifier-leak scanner, three seats.** `tests/_leak_guard.py` replaces the guards that embedded the
  very hostnames/domain they blocked (`test_no_topology_leak`, `test_gen_homepage`, `test_mgmt_parameterization`,
  the P-1 `validate` leg): a structural layer (RFC-1918 in dotted/dashed/underscored form — the dashed capture
  filenames in `tests/` had slipped every earlier guard — non-documentation MACs/IPv6, real-length age keys,
  the personal e-mail, operator-machine paths) plus an instance-token layer read from the private
  `instance/leak-tokens.txt` / the CI secret `KONTROLL_LEAK_TOKENS` / the shipped canaries, reported by index +
  digest, never the name. Runs as `validate` step `pii-guard`, the new `pii-guard.yml` workflow (*No leaked
  personal identifiers*) and `tests/unit/test_leak_guard.py` (incl. a canary self-test and a whole-tree scan).
  `kontroll-init --fresh` scaffolds the token list under its live name.
- **The shippable tree is identifier-free.** Every real address, hostname, domain, VLAN id, Proxmox token id,
  operator path and the real switch username in docs, fixtures, role headers, comments and this CHANGELOG now
  uses the shipped example inventory's names and TEST-NET addresses (`my-firewall 192.0.2.1`, `my-switch
  192.0.2.2`, …, `<mgmt-ip>`, `<your-domain>`); PLAN.md §4/§6/§8/§11/§13, README "Current status", the
  SECURITY threat model and CLAUDE.md no longer describe one private lab. The gate: zero findings with the
  private token list active.
- **CI runs anywhere.** `runs-on` is the variable `KONTROLL_RUNS_ON` (default GitHub-hosted `ubuntu-latest`;
  the private repo keeps the org runners). `validate` runs `--strict` with every tool installed in the job
  (sha256-pinned sops 3.13.1, gitleaks 8.30.1, promtool 3.5.4, vector 0.56.0 — promtool and vector-config had
  been silently SKIPPED on every green run since 2026-06-30); pip cache; gitleaks 8.18.4→8.30.1 everywhere
  (one `gitleaks:allow` on an env-var-NAMES line its newer rule trips); Dependabot gains a 7-day cooldown,
  grouping, PR limits and the `tests/` pip ecosystem.
- **Public-repo scaffolding:** `LICENSE` (MIT), `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` (Contributor Covenant
  2.1), PR + issue templates, `THIRD-PARTY-NOTICES.md` (the vendored MIBs, grafana.com boards, IEEE OUI data),
  a private vulnerability-reporting section in SECURITY.md, `docs/reviews/README.md`.
- Tests: `test_fleet_edit`'s placement test now writes its own shaped fleet (it had asserted the private fleet
  file's layout); `test_leak_guard.py` (8 tests), `test_paths_overlay.py` (+4), `test_kontroll_init` pins the
  token-list scaffold; the 10.x / `Vlan11` fixtures moved to TEST-NET / `Vlan100` with one `pii-guard: allow`
  (the private-unicast acceptance case). `docker/code-manifest.lock.yml` regenerated (paths/keygen/onboard).
- **Homepage editor on an unconfigured tree (found by the public cut's first GitHub-hosted e2e run):** the board
  files are a literal `instance/dashboards/homepage/` path (not `_OVERLAY_MAP`), so with no `instance/` the editor
  opened with zero sections. `service/homepage._read` gains the same narrow READ-only tier: the shipped
  `instance.example/` board when no `instance/` dir exists; writes stay on the overlay path and fail closed there,
  so the example is never a write target (two new tests in `test_homepage.py`).
- **Test-isolation fix:** `test_onboard_ssh_key::test_apply_routes_creds_through_stdin_never_argv` stubbed every
  writer except `gitio.write_inventory_host`, so every suite run wrote a real `instance/inventory/onboarded-cisco_ios.yml`
  (host `sw1`, TEST-NET) into the checkout — the untracked "fossil" the 2026-10-08 review took for an operator
  artefact (S03). Now stubbed; the file is a test artefact and was removed from the working tree.

### fix(security): a staging service can no longer push origin→main (C10 origin-push breach) (2026-07-05)

Design-of-record: `docs/reviews/2026-07-05-c10-origin-push-breach/`. A live dogfood on the SECURE edition (VM 144,
`trust_mode: separated`, `KONTROLL_STAGE_PUSHES=1`, an experienced-operator Blind-Joe against a real Cisco C9300)
caught a **C10 breach**: the documented staging flow (`apply:true, push:true`) advanced canonical `main` from the
network service with **no human promote** — defeating propose-then-promote. Root cause: the content clone's `origin`
**is** the canonical (`git clone file:///srv/kontroll.git`), so `commit_and_push`'s optional `git push origin HEAD`
(the `push_origin` leg, threaded from the client `push` flag) fast-forwarded canonical `main`; the client `push`
flag conflated "stage it" (already done by the `local` push to `proposed/<run_id>`) with "push origin". Proven by a
controlled experiment (`push:true` moved main, `push:false` did not) and universal across all 16 network-service
write routes (10 GUI + 6 API), which funnel through the one shared primitive. **Fix:** `origin = (not staging) and
bool(push_origin) and _run(...)==0` in `gitio.commit_and_push` — a staging service now has **no reachable origin
push** (the operator-CLI direct-main path is byte-identical to before). **Defence-in-depth:** a uid-1001-scoped
`update` hook on the canonical (`ansible/playbooks/files/canonical-update-hook.sh`, installed by `local-canonical.yml`)
refuses a receive-pack push to `refs/heads/main` from the network service — the legit mirror push (uid 0 in the
installer container, or the operator uid host-direct, never 1001) and `promote_ref`'s `update-ref` (not receive-pack)
are untouched. Severity **HIGH** (a core-C10 invariant breach) but bounded: armed surfaces only (read-only deploy
unaffected), mgmt-VLAN, FF-only (the clone is always rebased onto canonical main before the push — never a force),
no secret exposed, the canonical is a reconstructible cache. On the homelab `solo` tine the same push had been
**bypassing** the auto-promoter's blast-tier gate — the fix routes every `main` advance back through `promote_ref`,
making `solo` strictly more correct. Why it stayed hidden: unit tests patch the single `_run` seam (never a real
`origin==canonical` repo), and the prior Blind-Joe ran on the permissive `homelab` tine where `main` advancing is
expected — only the secure-edition run could surface it. Tests: `test_staging_isolation.py` extended with a real-git
`origin==canonical` topology — a staging `push_origin=True` asserts `proposed/<run_id>` created **and** canonical
`main` UNCHANGED (FAILS pre-fix); the operator-CLI case still advances main; the committed hook artifact refuses a
uid-1001 push to main but allows proposals + the non-1001 mirror + the `update-ref` promote. Docs: `SECURITY.md`
(C10 control + two accepted-risk rows), `docs/privileged-mutation-enablement.md`, `docs/trust-mode.md`. Deferred
(tracked): the offsite-remote server re-check (enforce the GUI `has_remote` checkbox server-side) and H2 (convert
the deploy mirror to `update-ref` so the hook can be unconditional). Edition-neutral on `main`; the homelab fork
inherits it on rebase.

### feat(gui): a Discard button to reap a stale proposal (FF-race Move 2) (2026-07-05)

The reaper half of the FF-race remediation (design-of-record `docs/reviews/2026-07-05-ff-race-parked-proposals/`).
A proposal `main` advanced past can never be promoted (non-fast-forward), so it lingers in the Pending list as dead
weight. A **stale-only** Discard control (behind a two-step confirm) → `POST /api/pending/<run_id>/discard` → a NEW
`scripts/kontroll/service/discard.py::discard_proposal` deletes its `proposed/<run_id>` ref from the canonical.
**C10-safe by construction:** un-stage ≠ promote — it removes Key-1's OWN output; it cannot advance `main`, enact
anything, or grow the promotable set (the GUI still has no promote path). The write verb lives in its OWN module so
`pending.py` stays strictly read-only, and is registered in `WRITE_VERBS` in the same commit (the fail-closed
completeness gate fails CI until it is). Security must-fixes folded in from the two adversarial reviews: **SEC-B1**
the `run_id` validator accepts EXACTLY the two real minter shapes (`uuid4().hex[:12]` and the API
`YYYYMMDDTHHMMSSZ-<6hex>`) and refuses a git flag / path traversal / branch name, and the ref is passed after
`git update-ref -d --end-of-options` so a leading-`-` leaf can never be read as a flag; **SEC-M1** compare-and-delete
on the tip read first (a concurrent promote/rebase that moved the ref → 409, a vanished/never-staged ref → 404, a
bad id → 400, never a 500); **MF-4** the delete argv is a single literal list so the read-only completeness detector
sees the verb. Griefing is availability-only (an authed user could delete a legit pending proposal — the same trust
tier the propose surface already grants; stale-only + confirm bound it; recovery is from the audited sha).
Tests: `test_discard.py` (both-minter validator + injection refusal, compare-and-delete/TOCTOU, the argv-injection
AST plant, the completeness-gate-names-it proof) + route tests (auth + 400/404/409 mapping + the audit lines) +
an e2e (confirm-gated delete, the error-leaves-the-row case, the promotable-has-no-discard negative). Docs:
`testid_reference.md`, `SECURITY.md` (C10 rider + accepted residual), `logging-architecture.md` (the new audit
action). Edition-neutral on `main`; the homelab fork inherits it on rebase.

### feat(gui): a Re-propose button on a stale proposal (FF-race Move 1) (2026-07-05)

Follow-through on the FF-race remediation (design-of-record `docs/reviews/2026-07-05-ff-race-parked-proposals/`).
Part 1 (#144) *badges* a non-fast-forwardable proposal "stale — re-propose"; this wires that prose to a working
button. On a stale **onboard** proposal the Pending panel now shows a `pending-re-propose` control that closes the
panel, sets a context banner, and searches the proposal's device CLASS — landing the operator on the SAME onboard
dialog to re-run it against current `main` (a fresh proposal that IS a clean fast-forward). It is a **navigation
affordance, not an actuation**: no promote, no stage, no new write verb — the GUI still cannot advance `main` (C10
two-key intact). `pending.list_pending` gains a read-only `repropose_hint` derived **only** from the changed-path
NAMES it already returns (`onboarded-<key>.yml` → the class key); host/group/creds live INSIDE the inventory YAML,
never in a path name, so they are deliberately NOT reconstructed — the human re-enters them (SOPS is irreversible,
and a base-moved HIGH proposal wants that re-review). The read-only-by-construction pin stays green (pure string
work, no git/write). Reuses the discovery-inbox "Onboard this → search" navigation verbatim. Covering unit test
(the path-name derivation + the MF-2 boundary) + an e2e (stale row → button → onboard form routed to the class);
`testid_reference.md` updated. Edition-neutral; no `SECURITY.md` change (no new trust boundary).

### fix(observability): config generators merge the onboard inventory drop-ins (2026-07-05)

A live homelab dogfood (a blind non-developer onboarding a real Cisco C9300 through the GUI for SSH control + SNMP
telemetry + SSH config-backup) surfaced a real gap. The onboard writes a host to an ADDITIVE drop-in
`instance/inventory/onboarded-<key>.yml` (never the curated `hosts.yml`), and ansible reads the inventory
DIRECTORY (`ansible.cfg inventory = ../instance/inventory`) so every playbook manages it — but the standalone config
generators loaded `hosts.yml` as a SINGLE file. A GUI-onboarded device therefore got **NO Prometheus scrape target,
NO Homepage tile, NO logging source**: the onboarded switch's SNMP target came out as the placeholder `192.0.2.2`,
never the real `<switch-mgmt-ip>`. New `kontroll/inventory.merged_inventory()` folds the `onboarded-*.yml` drop-ins into the
`hosts.yml` shape (curated host wins on a name collision; read-only glob+load), wired into all four inventory-reading
generators — `gen-observability`, `gen-homepage`, `gen-logging`, `gen-validate-live`. The committed `--example`
lockfiles are byte-unchanged (`instance.example/` ships no drop-ins, so the merge is the identity there). Covering
test `tests/unit/test_inventory_merge.py` pins the merge, the collision rule, and a real onboarded SNMP target.
Edition-neutral (affects both tines). Dogfood-proven live: the C9300's SNMP target regenerated and Prometheus
scraped `<switch-mgmt-ip>`.

### feat(gui): a hostname field on the onboard form (2026-07-05)

A live dogfood found that a homelabber onboarding a device through the browser couldn't NAME their host — the onboard
form had `key/group/host/secrets` but no inventory-name input, so a browser-only user got the server's auto-name (e.g.
`cisco_ios-1`) while only the typed API could pass `host_name`. Added a `field-host-name` input (`name=host_name`,
optional; blank ⇒ the server auto-names `<key>-N`, exactly as before), threaded into the submit body and the
`onbPristine` collapse-gate, and PRE-FILLED from a Discovery row's hostname when onboarding via "Onboard this" (a form
value only — `input.value` is text and the server validates `host_name`; consistent with the F6 host pre-fill, and NOT
a class guess so the F8 guard is untouched). `tests/testid_reference.md` updated + the discovery-flow e2e now asserts
the hostname pre-fill. GUI-only, edition-neutral.

### fix(gui): badge stale (non-fast-forward) proposals in the Pending panel (2026-07-05)

A live dogfood exposed a fast-forward race: when the homelab auto-promoter promotes a LOW-blast proposal (advancing
canonical `main`), an earlier-staged HIGH-blast proposal (a device onboard left for a human) becomes non-FF and its
promote is then silently REFUSED by the FF-only gate — with nothing in the GUI telling the operator. `pending.
list_pending` now carries a read-only `promotable` flag per proposal (`git merge-base --is-ancestor refs/heads/main
<ref>` — the EXACT check the promote gate makes; the AST pin already whitelists that verb, so the C10
read-only-lister guarantee holds). The Pending panel badges a non-FF proposal **"stale — re-propose"** (`pending-stale`)
and replaces the would-be-refused promote recipe with re-propose guidance. Edition-neutral — it also helps main's
separated mode (an operator no longer clicks promote and hits a cryptic non-FF refusal). Read-only; no new write verb.
Covering test pins the flag both ways over a real FF-race, plus the template wiring. Follow-up from the Blind-Joe
Cisco dogfood; the homelab-tine auto-rebase + a discard button are separately tracked.

### feat(discovery): OUI vendor enrichment — the pinned IEEE MAC→vendor display hint (2026-07-04)

Corpus-growth Rung 3 (the LAST of the OpenWrt/IPv6/OUI blackboard), a clean instance of the repo's derive→pin→--check
doctrine. The discovery inbox now annotates a candidate's MAC with its **manufacturer** ("Raspberry Pi", "Ubiquiti",
…) — a recognition aid for the human deciding which device class to search for. It is a **DISPLAY HINT, never a class
guess** (SECURITY C18 F8): the vendor is inert text on a row the operator already sees; no code reads it to pick a
collection or synthesize an onboard card.

New `scripts/gen-oui.py` (a `gen-dashboard-floor.py` clone) reduces the WHOLE public IEEE MA-L/MA-M/MA-S registries to
a pinned `oui/oui-lookup.generated.json` (prefix→vendor, registrant addresses dropped; ~53k prefixes, ~2 MB) + a
byte-exact `oui-lookup.lock.yml`. The reader (`service/discovery._vendor_for`, pure) does **longest-prefix match**
(36→28→24 = probe 9→7→6 nibbles) — IEEE re-parcels a /24 into /28+/36 blocks, so a naive first-3-bytes lookup ships
the WRONG vendor for a small-block device. The candidate schema gains an advisory `vendor` (may be None); the GUI
renders it via `ET`/textContent.

**BRICK-1:** `--refresh` (the only network leg, hits `standards-oui.ieee.org`) is operator/CI-authoring only; validate
+ deploy run only the OFFLINE `--check` (schema + 6/7/9-hex keys + honest count + sha256 tamper floor + exact
sources), and a new `oui-refresh-not-in-deploy` static grep-gate proves the network verb never appears in an install
path (mirrors `dashboard-floor-resolve-not-in-deploy`). **Read-only AST pin stays green** (`_load_oui`/`_vendor_for`
are pure committed-file reads); code-manifest regenerated (`service/discovery.py` + `paths.py`). Baked (`COPY oui/`),
gitleaks-allowlisted (both generated files). **F8 un-regressable:** an e2e asserts "Onboard this" pre-fills ONLY the
host — no class pre-picked from the OUI. Surfaces NO new hosts (display polish, not corpus growth). The corpus-growth
arc (IPv6 → OpenWrt → OUI) is now COMPLETE.

### feat(discovery): OpenWrt forced-command SSH DHCP-lease discovery — the `dhcp_leases_openwrt` method (2026-07-04)

Corpus-growth Rung 2 (of the OpenWrt/IPv6/OUI blackboard). A new `dhcp_leases_openwrt` discovery method reads an
OpenWrt AP's OWN dnsmasq lease file (`/tmp/dhcp.leases`) read-only over a **forced-command SSH** key — the one-shot
twin of `logging_file_tail_ssh`'s `journalctl -f` grant, and the first `source.transport: ssh`. OpenWrt has no
read-only lease GET (ubus/LuCI are HTTP POST, banned by the passive-only gate and a *wider* file-read grant), so the
forced-command `cat` is the tightest read. The transport is a THIRD read-only fetcher `ssh_read` (streamed +
killed-on-overflow byte cap, mirror of `snmp_walk`) that sends NO client command word (the argv ends at
`<user>@<host>`), so the AP's `authorized_keys` forced command is the only thing that runs; host-key **pinned** (no
TOFU), `BatchMode`/`IdentitiesOnly` (no password/agent fallback). Plus a `lease_file` parser + a one-line offer on
`modules/openwrt`. The read cred is a **DEDICATED** forced-command key in the `network` SOPS domain
(`openwrt_discovery_ssh_private_key`) — **never** the full-shell `ansible_ed25519` root key — authorized by the new
`ansible/roles/openwrt_discovery_key` wiring role (`no_log`, `--check`-safe, idempotent). The control image now
`apk add`s `openssh-client`.

**Security (a fresh OW-GATE adversarial pass, `docs/reviews/2026-07-04-openwrt-ssh-gate/`):** the
`discovery-passive-only` gate gained a case-SENSITIVE POSITIVE leg forbidding every write/tunnel/remote-exec SSH
construct — transfer verbs, the `Command`/`Forward` option families, `ProxyJump`/`Tunnel`/`Subsystem`, the
`-F`/`Include=` config-indirection, and the `os.system`/`os.popen`/`pty`/`paramiko`/`os.exec*` alternate-spawn
primitives — closing the two bypasses the sketch missed. The load-bearing backstop a grep can't be is
`test_ssh_read_is_forced_command_read_only` (an argv-AST proof: single unmutated static-list argv, no splat, the host
token is LAST with nothing after it, no `shell=True`, the hardening options present) +
`test_actor_has_no_shell_true_and_no_extra_ssh_spawn`. Read-only AST pin stays green + zero code-manifest regen
(`service/discovery.py` untouched — a v4 dnsmasq `{ip,mac,hostname}` row flows through the existing guards). SEE-ONLY
caveat inherited (a v6 lease would pre-fill but onboard stays v4). Accepted-risk **R-DISC-2**; C18 updated;
`discovery/README.md` carries the host-execution decrypt-to-`0600`-file provisioning flow. No live-device dogfood yet
(confirm-first). (OUI enrichment is Rung 3, gated.)

### feat(discovery): IPv6 dual-stack neighbour discovery — the `neighbors_snmp` method (SEE-ONLY) (2026-07-03)

Corpus-growth Rung 1 (of the OpenWrt/IPv6/OUI blackboard, `docs/reviews/2026-07-03-discovery-corpus-growth/`). A new
`neighbors_snmp` discovery method walks the **RFC-4293 dual-stack ipNetToPhysicalTable** (`1.3.6.1.2.1.4.35.1.4`) over
the SAME SNMPv3 transport + creds as `arp_neighbors_snmp`, surfacing IPv4 **and IPv6** ND neighbours the v4 ARP table +
DHCP-lease APIs never see. `arp_neighbors_snmp` (v4) is kept byte-identical (the live cisco dogfood stays valid);
cisco_ios now offers both. The one new code add is the `neighbors_table` parser (`_index_to_ip` decodes the
TYPE-tagged/LENGTH-prefixed InetAddress OID index → v4 or canonical v6). The read side gains a **dual-stack key guard**:
new `_v_ipv6` + `_v_ip` validators, and `_clean_ip` now asks for `ip` — but `_v_ipv4` is **untouched**, so the
config-plane danger knobs (`mgmt_ip`/`ansible_host`, `type: ipv4`) keep their v4-only guard and the read-only AST pin
stays green. The `discovery-p1-fixtures` gate gained a v6-aware OID strip (a doc v6 index no longer false-trips) + a
new RFC-3849-allowlisted v6 identifier leg. **SEE-ONLY:** a v6 candidate surfaces + pre-fills but onboard stays v4 (a
v6-onboard is a separate config-plane decision). No new transport/fetcher/secret/device-authority. (OpenWrt SSH +
OUI enrichment are scoped as Rungs 2–3, gated.)

### fix(discovery): parse the collapsed-leading-zero net-snmp MAC form in arp_neighbors_snmp (2026-07-03)

`_hexmac_to_colon` required exactly 2 hex digits per octet, but net-snmp renders `ipNetToMediaPhysAddress` **without**
the IP-MIB PhysAddress DISPLAY-HINT on the runtime image (it ships `net-snmp-tools`, not the MIB files) — so a sub-0x10
octet collapses to a single digit (`STRING: 0:0:5e:0:53:1`), which the parser rejected → the neighbour surfaced
with `mac=None`. ~32% of real MACs have at least one sub-0x10 octet, so this silently dropped MACs on real switches.
Fix: accept `{1,2}` hex per octet and zero-pad each to 2 digits (`00:87:…`). Live-caught on the cisco SNMP-ARP dogfood
(4/5 neighbours matched the switch's real ARP table byte-exact; the 5th, a `00`-octet MAC, was the miss). Covered by
`test_hexmac_to_colon_normalizes_and_rejects` with the exact live bytes.

### fix(docker): ship net-snmp in the control image so the SNMP transport works on a baked deploy (2026-07-03)

The control image is `FROM semaphoreui/semaphore` (Alpine) and installed only `age sops` — **no net-snmp**. But the
discovery `snmp_walk` transport (`scripts/kontroll-discover.py` → `snmpbulkwalk`) and `gen-validate-live`'s SNMPv3
GETNEXT (`snmpgetnext`) both shell out to the net-snmp CLI. Absent, an SNMPv3 walk raises `FileNotFoundError`, which
the fetcher catches and (mis)records as `unreachable` — so the **entire SNMP-ARP discovery transport is silently dead
on every baked deploy** (and gen-validate-live's SNMP validation too). Fix: `apk add … net-snmp-tools`; guarded by
`test_control_image_ships_net_snmp_cli`. Caught + proven by the cisco SNMP-ARP dogfood (a net-snmp responder VM: the
`snmp_walk` walked a real SNMPv3 `ipNetToMediaTable` only once the CLI was in the image).

### fix(discovery): the sweep's audit-log default writes under write_root(), not the baked-read-only ROOT (2026-07-03)

`kontroll-discover.py::_audit_path()` fell back to `os.path.join(ROOT, "local", "discover-audit.log")`. On a
Phase-B **baked** deploy ROOT is the read-only `/opt/kontroll` image tree, so the per-source audit write raised
`PermissionError` and aborted the **whole** sweep (the device fetch had already run, but the audit line crashed it).
Fix: fall back under `paths.write_root()` — the writable propose clone — exactly mirroring `inbox_path()`; identity
when `write_root()==ROOT` (the legacy `/repo` deploy is byte-unchanged). Live-caught on the baked-box discovery
dogfood: the sweep read the real FortiGate's 15 DHCP leases but the artifact never landed until the audit path was
redirected. Covered by `test_discover_audit_path_defaults_under_write_root`.

### fix(docker): bake the discovery + dashboard-floor registries into the control image (2026-07-03)

Two post-Phase-B data registries — `discovery/` (the passive lease-source registry, read by the sweep + the GUI
Discovery panel) and `dashboards/derived/` (the Rung-4 board-floor lock pins, read by `classify._derive_dashboards`
on every onboard) — were added to `paths.py` (both resolve under the baked ROOT) but **never wired into the control
image's `COPY` set**. On a **baked** deploy (`bake_code=true`, the published-image path) their runtime readers hit a
missing `/opt/kontroll/<dir>` → the Discovery panel/sweep served an empty registry and derived classes got no
dashboard floor. The prod box (v0.1.1) predates both features, so nothing had exercised the gap yet; it blocked any
baked deploy of discovery. Fix: `COPY discovery/` + `COPY dashboards/derived/` (the committed pins only — **not** the
generated `dashboards/grafana` JSONs). The bake-completeness guard (`test_publish_images.py`) hard-coded the registry
list and caught neither miss — it now **derives** the required set from `paths.py`'s ROOT-relative `*_DIR` constants,
so a future registry that isn't baked fails CI loudly instead of shipping an empty panel to prod.

### fix(discovery): live FortiGate dogfood — drop the inert `secret_env_map`, document the export step (2026-07-03)

A Blind-Joe (docs-only) live dogfood against the FortiGate edge (FortiOS v7.2.13) **confirmed** the DHCP shape exactly
(GET `/api/v2/monitor/system/dhcp` → `{results:[{ip,mac,hostname,...}]}`; 15 real leases parsed 15/15 with all three
fields — no `parse` change needed) and surfaced three real cred-wiring bugs the hermetic tests couldn't:
- **The `secret_env_map` on the discovery descriptors was inert AND unhardcodable** — `gen-secret-env` renders `.env`
  from `metrics:`/`logs:` only (never `discovery:`), and the real SOPS field name is **instance-generated by onboard**
  (`<ns>_<coll>_<N>_<field>` — the live token is `fortinet_fortios_1_api_token`, not the descriptor's guessed
  `fortigate_discovery_token`). Removed `secret_env_map` from all four discovery descriptors; creds are now honestly
  documented as **operator-exported at invocation**, with the export step added to `discovery/README.md § Extending`.
- **The reuse-the-existing-token premise held once the device was configured for it** — the FortiGate 403 was a
  **trusthost** gate (the control node wasn't in the API admin's trusthost), not a permission-scope failure; noted as
  a device prerequisite in the descriptor + README. FortiOS also rate-limits repeated auth (429) — the sweep does one
  GET per source (gentle) and records a non-200 as a status, never retries.

No code change (the `json_rows` parser + the shape were validated as-is); descriptors + docs only. Findings record:
`local/dogfood-fortigate-discovery-findings.md` (git-ignored; no live identifiers).

### feat(discovery): SNMP-ARP neighbour discovery — the `snmp_walk` transport (Rung 3) (2026-07-03)

The passive-discovery sweep gains a SECOND transport so it can read a Layer-3 switch's OWN ARP (IP↔MAC neighbour)
table read-only over SNMPv3 — surfacing STATICALLY-addressed hosts and cross-VLAN neighbours that no DHCP-lease API
sees. New method `discovery/arp_neighbors_snmp.yml` (`source.transport: snmp`, walks the ipNetToMediaPhysAddress
column `1.3.6.1.2.1.4.22.1.2`), offered via a declared `discovery:` block on `modules/cisco_ios/module.yml` (the live
core switch). Unlike the Rung-2 vendor siblings this is NOT codeless — it adds, in the top-level actor
`scripts/kontroll-discover.py` (OUTSIDE the pinned package, so the read-only AST pin AND the code-manifest stay green
UNCHANGED — the zero-new-authority story holds): a `source.transport` discriminator (default `http`, so the three
DHCP `json_rows` methods are byte-identical), a read-only `snmp_walk` fetcher (net-snmp `snmpbulkwalk` — GETBULK,
never a SET verb), and an `arp_table` parser (the IPv4 is the OID-suffix, the MAC the value; incomplete/expired ARP
rows dropped). Design-of-record + adversarial review (both reviewers GO-WITH-FIXES):
`docs/reviews/2026-07-03-discovery-rung3-snmp-arp/99-synthesis.md`.

Security (SECURITY C18 generalized, R-DISC-1 widened — same boundary under a second transport): the walk is
**byte-bounded at the transport** (a streamed, killed-on-overflow read of the net-snmp child — a hostile/huge ARP
table cannot OOM the control VM), reaches ONLY the onboarded switch's `:161` (never the network), reuses the shared
read-only `snmp_observability` v3 cred AS-SCOPED (operator-exported `KONTROLL_SNMP_V3_*`, unset ⇒ `no-creds`), and
raises cred-free (stderr discarded; the net-snmp `-A`/`-X` argv residual is accepted under C3). The
`discovery-p1-fixtures` gate is made **OID-aware** (a numeric OID's sub-identifiers no longer false-positive as
dotted-quads, while a genuine live IP in an OID suffix is still caught) + gains a Hex-STRING MAC leg — the net-snmp
golden fixture is P-1-clean. Full hermetic tests (parser reconstruction, hostile-output tolerance, the transport
dispatch, the real-fetcher byte-cap, no-creds/unreachable, a read_inbox round-trip). F11: the OID/shape is the
documented public IP-MIB, pinned vs a mocked fixture; a live-cisco_ios dogfood confirms/corrects with a 1-line edit.

### feat(discovery): vendor DHCP-lease discovery siblings — fortigate + routeros (Rung 2) (2026-07-03)

Two new passive-discovery methods, added as **pure file-only drop-ins** with **zero** change to
`scripts/kontroll-discover.py` (the codeless-modularity contract, proven — the code-manifest lock is untouched):
`dhcp_leases_fortigate` (the FortiOS monitor API `GET /api/v2/monitor/system/dhcp`, a `results`-keyed body, a `custom`
`Authorization: Bearer <token>`) and `dhcp_leases_routeros` (the RouterOS v7 REST API `GET /rest/ip/dhcp-server/lease`,
a **bare** top-level JSON array with no wrapper key, HTTP Basic). Each reuses the existing `json_rows` parser — the
sole differences are descriptor DATA (`rows:`/`fields:`/`auth.strategy`), so the "add a discovery source = one file"
claim now holds across three vendors and two auth strategies + two body shapes. The offer is a DECLARED `discovery:`
block on `modules/fortigate/module.yml` and `modules/routeros/module.yml` (opt-in per class — fortigate ships httpapi
yet the offer is still declared, so onboarding a firewall never silently starts reading its leases). This gives the
**live** pre-cutover edge (FortiGate) a usable discovery source for the first time; routeros ships dormant (staged
until the post-cutover core migration). Same passive/read-only/cred-reuse trust boundary as `dhcp_leases_opnsense`
(SECURITY C18 unchanged — no new control/secret/domain; both reuse the `network` SOPS domain AS-SCOPED). New
hermetic fixtures + unit tests (registry order, both declared offers, the `results`-key & bare-array parsers, the
Bearer & Basic sweeps end-to-end). F11 (dogfood-confirmed-not-CI-confirmed) carries over per vendor: the shapes are
the documented public APIs, pinned against mocked fixtures; a live dogfood confirms/corrects each with a 1-line edit.

### feat(discovery): the discovery-inbox GUI panel + onboard host pre-fill (Rung 1b) (2026-07-03)

The discovery inbox gains its operator surface: a read-only **Discovery** panel (`GET /api/discovery` →
`service/discovery.read_inbox`, mirroring `/api/pending` — a pure read of the git-ignored cached sweep artifact,
reaching no device) listing the un-onboarded hosts the last out-of-band sweep saw, and a per-row **"Onboard this"**
that stashes the discovered host and pre-fills it into the EXISTING onboard form. Because Rung 1 does not guess the
device class (F8), the operator searches + picks the class, then the next onboard form's `field-host` is pre-filled
with the discovered IP (a visible, clearable banner is the indicator — never a silent stash). The human still
onboards + promotes every host (C10 — **no auto-onboard**; discovery only feeds the existing flow). Every device
byte (hostname/ip/mac) is rendered via `ET`/`textContent`, so a `<script>` in an attacker-influenceable lease
hostname renders as literal text and never executes (F6). New `discovery-*` testids, GUI route tests
(`test_gui_api.py`: 401 / mocked-200 / degrade-500), and an e2e (`tests/e2e/test_discovery_flow.py`: the panel
renders, the `<script>`-hostname-as-text XSS guard, the pre-fill lands in `field-host`). SECURITY C18 updated to
"Rung 1a+1b built". This completes the discovery-inbox build arc; the confirm-first live-OPNsense dogfood is the
only remaining (deferred) step.

### feat(discovery): the passive discovery inbox — the read spine (Rung 1a) (2026-07-03)

A new 4th self-describing registry, `discovery/<method>.yml`, isomorphic to `telemetry/`/`logging/`
(`catalog.load_discovery` + `paths.DISCOVERY_DIR`), that lets an already-onboarded device be read for its OWN
DHCP-lease / neighbour view read-only, so the **un-onboarded** hosts on the network can be surfaced into an onboard
*inbox*. A human still onboards + promotes every one — there is **no auto-onboard** (C10 unchanged); Rung 1a is the
server-side read spine only (the GUI inbox + onboard-form pre-fill are Rung 1b). The load-bearing architecture call:
the device reach + the artifact WRITE live in a **top-level actor** `scripts/kontroll-discover.py` (a GET-only,
byte-capped fetcher — no write/scan primitive — that clones `gen-validate-live`'s read-only transport, dispatches
over a `parse.shape`-keyed `_PARSERS` table with **zero vendor branch**, count-caps the rows, and writes the
git-ignored `local/discovery-inbox.generated.json`), while `scripts/kontroll/service/discovery.py` is a **pure**
`read_inbox()` — it loads that artifact + `fleet._inventory_groups()`, re-validates every field through the closed
`_validate` guards, IP-diffs out already-onboarded hosts, and returns the candidates. Keeping the write out-of-band
is what lets `read_inbox` stay `assert_read_only`-green (the read-only AST pin is **unchanged** — the zero-new-authority
proof). The one shipped method `dhcp_leases_opnsense` is offered via a **declared** `discovery:` block on the opnsense
class (not a facts predicate — opnsense ships no httpapi plugin, so a predicate would reach nothing). New SECURITY
control **C18** (+ accepted-risk **R-DISC-1**), two `tests/validate.sh` grep-gates (`discovery-passive-only` +
`discovery-p1-fixtures`), and `tests/unit/test_discovery.py`. Design-of-record:
`docs/reviews/2026-07-03-discovery-inbox-scope/99-synthesis.md`.

### feat(modules): wire the derived dashboard floor onto every derived class — Rung 4 COMPLETE (north-star Rung 4a-2) (2026-07-02)

Every `derived: true` device class now AUTO-CARRIES its Grafana board floor: `classify.derive_class_capabilities`
gained a `dashboards` key (`_derive_dashboards` emits a `{gnet, name}` for each derived telemetry method that carries
a `derive_dashboard:` selector + a resolved lock, deduped, honesty-gated — a board rides only the series its method
emits), and `gen-class-capabilities --check` extends its drift gate to a **3rd capability** (`metrics`/`logs`/
`dashboards`) so a derived class's board floor can't rot. The derived `dashboards:` blocks landed on all six classes:
**cisco_ios** `[snmp_if, blackbox]`, **fortigate** `[blackbox]` (snmp stays a bare override → no snmp board),
**docker_host** + **openwrt** `[blackbox, node]` (docker_host's curated `{1860}` node board is now the derived floor),
and staged **routeros** `[snmp_if, blackbox]` / **opnsense** `[blackbox, node]` (frozen-oracle-gated until cutover).
INVARIANT-D holds: **onboard writes the metrics/logs floor but NOT a `dashboards:` block** — a board rides the
out-of-band-resolved locks, so a blind onboard's board is re-derived by the operator (the honest gap names it); the
onboard except-fallback gains `"dashboards": []`. New `catalog.load_dashboard_locks()` + `paths.DASHBOARD_LOCKS_DIR`.
This closes north-star **Rung 4** (and the whole de-bespoke onboard arc): even the dashboard is derived + pinned.

### feat(dashboards): the cloud-derived dashboard-floor ENGINE — even the board id is derived (north-star Rung 4a-1) (2026-07-02)

The dashboard floor now obeys the no-bespoke tenet: a telemetry method declares a `derive_dashboard:` SELECTOR (a
grafana.com search query + datasource + a required series + a soft `prefer_gnet` tie-break — **NOT a hand-typed
`gnet`**) on its `telemetry/<method>.yml`, and the new `scripts/gen-dashboard-floor.py` DERIVES the concrete board id
from the parseable public registry, deterministically ranks it (datasource hard-filter → prefer_gnet → downloads →
id-ascending), verifies the pinned board actually queries the required series, and PINS `{id, revision,
content_sha256}` into `dashboards/derived/<method>.lock.yml`. Three floor selectors ship resolved: **host_node → gnet
1860 "Node Exporter Full"** (rev 45; re-pins the existing `node.json` **byte-identically** — the inert proof),
**snmp → gnet 1124 "SNMP Interface Throughput"** (`snmp_if.json`, new), **blackbox → gnet 13659 "Blackbox Exporter
(HTTP prober)"** (`blackbox.json`, new). The **BRICK-1 split** is total: `--resolve` is a network operator/control-VM
tier (egress-only, no credential), while the install-gating `--check` is hermetic — it re-hashes the committed board
against `content_sha256` (the sha256 tamper floor) and re-greps for the required series OFFLINE. A `validate.sh`
static gate keeps `--resolve` out of `deploy-stack.yml` + CI; a board swap warns loudly (MF-5). This is the ENGINE
only — no class wiring yet; the derived `dashboards:` blocks on the migrated classes land in 4a-2.

### feat(modules): docker_host joins the auto-derived floor — the declared-backend proof (F2, north-star Rung 4) (2026-07-02)

`docker_host` is now `derived: true`, completing the Rung-4 vendor-class migrations (all curated classes now
self-describe). It is the one class that does NOT classify: community.docker ships a `docker_config` swarm module
whose `_config` suffix trips the `raw_ssh` backend's `none_of {module_suffix: _config}` guard, so
`predicate.classify` resolves NO backend. But a Docker host is a plain Linux box whose telemetry backend is raw SSH
(the collection only actuates containers), so the module DECLARES `backend: raw_ssh` and
`gen-class-capabilities._backend_for` honors a declared backend over classification → the derived floor is raw_ssh's
`[blackbox, host_node]`. **ADDITIVE on blackbox** (the class had `host_node` only → it gains a committed
`prometheus/targets/blackbox/docker_host.generated.yml`); `host_node` stays byte-identical; the `{gnet:1860}`
dashboard stays a bare curated override and backup stays role-gated. Frozen-oracle test pins the classify-failure
premise + the declared-backend derivation.

### feat(modules): openwrt joins the auto-derived floor — the empty-facts proof (F2, north-star Rung 4) (2026-07-02)

`openwrt` (the OpenWrt AP, `active`/LIVE) is now `derived: true`. It has no Ansible collection (raw SSH via
`ansible.builtin`), so its `facts.pinned.yml` is legitimately EMPTY — with no plugins/modules, `predicate.classify`
falls to the `raw_ssh` catch-all, which confers telemetry `[blackbox, host_node]`. **ADDITIVE + LIVE:** it gains a
committed `blackbox` (ICMP reachability) target AND a `node_exporter` target. The AP runs no node_exporter by
default, so the node target scrapes **HONEST-DOWN** (blanks a Grafana panel; there is no `up==0` alert rule, so it
raises nothing) — it is never faked away, and installing node_exporter on the AP turns it green. `host_node` is a
paradigm-level confer of `raw_ssh` (docker_host genuinely runs it) and is NOT special-cased off openwrt (one-seam).
New committed targets in `prometheus/targets/{blackbox,node}/openwrt.generated.yml`; frozen-oracle test +
`gen-class-capabilities --check` gate it. The one class whose pinned facts are empty-by-construction.

### feat(modules): routeros + opnsense join the auto-derived floor (F2, north-star Rung 4) (2026-07-02)

The two **staged** vendor classes are now `derived: true` with committed `facts.pinned.yml` (real probes of
community.routeros 3.21.0 + ansibleguy.opnsense 1.2.16). `routeros` ships a cliconf plugin → `netcommon_cli` → the
FULL network-gear floor (snmp + blackbox + syslog_push), the same as cisco_ios. `opnsense` ships **no connection
plugin** → the `raw_ssh` catch-all → `[blackbox, host_node]` (a real probe corrected the design's `api` assumption —
there is no httpapi plugin, only API-calling modules). Both are ADDITIVE (they carried no capability blocks) but
DORMANT (commented out of the fleet + inventory) → zero live targets and zero generated-artifact change until they
are enabled at cutover; while staged, their floors are gated by frozen-oracle unit tests (not
`gen-class-capabilities --check`, which only visits the enabled example fleet). Covering tests in
`test_derive_class_capabilities.py`.

### feat(modules): fortigate joins the auto-derived capability floor (F2, north-star Rung 4) (2026-07-01)

`fortigate` is now `derived: true` with a committed `facts.pinned.yml` (probed from fortinet.fortios 2.5.1). Its
`httpapi` plugin classifies to the `api` backend, which confers telemetry `[blackbox]` only — so the `blackbox`
reachability entry is now the DERIVED floor (marked `derived: true`, owned by `gen-class-capabilities --check`),
while the `snmp` interface-counter entry stays a **bare curated override** (`api` cannot confer SNMP on an httpapi
device — the honest residual, never faked). The migration is **byte-identical**: `gen-observability` reads
`method`/`params` only, so `prometheus/targets/**/fortigate.generated.yml` is unchanged. Covering frozen-oracle test
in `test_derive_class_capabilities.py`. First of the Rung-4 vendor-class migrations; the residual-isolation proof
shape (vs cisco_ios's full-floor proof).

### fix(onboard): a partial/cross-domain credential set is a clean 422, not a 500 (2026-07-01)

The onboard routes (`api/routes/onboard.py`, `gui/app.py`) now catch the planner's catchable `ValueError` —
raised by the MF-S2 domain-confinement guard or the auth_set coherence guard (a partially-filled required set) —
and return **422** with the guard's message, mirroring the existing `WriteConflict`→409 handling. Previously it
surfaced as a 500. A partially-filled set is a normal operator mistake, and F1 TIER-B (Rung 3) made it more
reachable by deriving required multi-field `auth_set`s (e.g. proxmox `api_user`+`api_token_id`+`api_token_secret`).
No creds are written on the refusal (the guards run pre-write). Covering tests in `test_api_onboard.py` +
`test_gui_api.py`; the guard's own behaviour stays pinned at the service layer (`test_onboard_proxmox_auth.py`).

### feat(onboard): TIER-B — derive the EXACT credential fields from the module `argument_spec` (F1, north-star Rung 3) (2026-07-01)

- **An ARBITRARY installed collection now yields its exact credential fields, not a coarse guess.** With `deep`,
  `authspec.auth_fields_from_argspec` reads the connecting module's `argument_spec` via `ansible-doc -j` and applies
  a small public HARD/SOFT heuristic (`no_log` → secret-name → non-secret locator) to emit the exact `CredField`
  set — e.g. proxmox's coherent `api_host`+`api_user`+`api_token_id`+`api_token_secret`, with the operational knobs
  (`name`/`memory`) dropped. When TIER-B yields a coherent set it SUPERSEDES the coarse per-backend shape
  (`cred_source: "deep"`); on any miss the TIER-A shape stands — never a blocked onboard (INVARIANT D*).
- **Two hardening gates (from the design's adversary pass).** **MF-S1** (install-confinement, FAIL-CLOSED): the
  `collection in local_installed()` check lives INSIDE `auth_fields_from_argspec`, before any `ansible-doc` call, so
  TIER-B can never introspect (execute) an un-installed collection's Python — a Galaxy result stays on TIER-A at
  zero code-execution cost. **MF-S6** (mask-ambiguous, FAIL-SAFE): `ansible-doc`'s `no_log` is not a complete secret
  oracle, so a genuinely-ambiguous required field defaults to **masked/secret**, never plaintext. No new code-exec
  surface (`deep_probe` already runs `ansible-doc`), no new install path, no new value-bearing surface — SECURITY.md
  updated (F1 Rung 3).
- **Wired end-to-end, opt-in.** `GET /api/onboard/cred-fields?deep=true` and `POST /onboard {deep}` thread it; the
  GUI requests `deep` on result-pick (install-confined server-side, so it's free for un-installed results). Touches:
  `scripts/kontroll/authspec.py`, `…/service/onboard.py`, `api/routes/onboard.py`, `gui/templates/index.html`,
  `tests/unit/test_authspec.py` (+10 cases incl. the MF-S1/MF-S6 pins).

### feat(onboard): coherent multi-field `auth_set` + proxmox names that feed the pve-exporter (F1 seam S1, north-star Rung 1) (2026-06-30)

- **Multi-field credentials are now ONE coherent `auth_set`, not loose boxes.** A curated device-class
  (`modules/<key>/module.yml`) may declare its own `auth:` block, which takes precedence over the generic
  per-backend shape on the reuse path (`authspec.derive_auth(…, module=…)`). The proxmox class declares its
  API token as one `auth_set` of three parts (user / token_id / token_secret); the GUI renders them inside a
  single `auth-set-proxmox_api` `<fieldset>`, and the planner refuses a **partially-filled** required set
  (you can't half-authenticate) while a values-free dry-run still plans (INVARIANT D*).
- **Closes the empty-Grafana dogfood symptom for PVE.** A new `shared: true` cred is a DOMAIN-level secret
  stored FLAT under its `sops_stem` (not host-keyed) — the proxmox token lands as
  `proxmox_api_user`/`proxmox_api_token_id`/`proxmox_api_token_secret`, the EXACT names `telemetry/pve.yml`'s
  `secret_env_map` reads, so onboarding a PVE node feeds the pve-exporter automatically (a cross-file test
  pins the name lineup). The curated `pve` exporter override (F2 Step 3) is thereby fed by S1's creds.
- **No new trust surface.** Pure config-as-data + a data-driven loop (no `ansible-doc` introspection — that's
  Rung 3). The MF-S2 domain-confinement guard still fires before any write; cred VALUES remain in-memory only
  (`creds_to_set`), names-only on every surface. `GET /onboard/cred-fields?collection=…` surfaces the curated
  set pre-plan. Touches: `modules/proxmox`, `scripts/kontroll/authspec.py`, `…/service/onboard.py`,
  `api/routes/onboard.py`, `gui/app.py`, `gui/templates/index.html`.

### feat(init): `trust_mode` — choose the promote posture at fresh-init (F3, north-star Rung 0c, flag only) (2026-06-30)
The optional two-tine trust fork's FLAG slice (Rung A of the F3 design). A new per-instance `trust_mode`
(`instance/instance.yml`) declares **who turns the C10 second key**: `separated` (a human runs the
`promote-proposal` Semaphore task — prod, today's system) or `solo` (homelab — declares intent to auto-promote).
It is chosen **explicitly at fresh-init and NEVER silently defaulted** (a trust posture is the one setting kontroll
refuses to guess — unlike `tls_mode`, which fail-SAFEs): the "never a default" property is defended in **three**
places — `kontroll-init --fresh` refuses without `--trust-mode {separated|solo}` (exit 1, the only place a fresh
overlay is born), the template ships `trust_mode` **commented**, and `deploy-stack.yml` **hard-fails** an
unset/invalid value (gated on the network-service tier). The chosen posture is surfaced **read-only** in the
Settings panel (`settings-trust-mode` — no edit knob). **NO auto-promoter is introduced** — the GUI/API trust
boundary is byte-identical in both modes (still propose-only, still AST read-only-pinned), so this rung adds **zero
new security residual**; choosing `solo` currently behaves like `separated` (proposals stay staged for manual
promote) until the gated auto-promoter rung ships. New `docs/trust-mode.md` (incl. the honest `solo` residual);
`SECURITY.md` C10 note; `tests/unit/test_trust_mode.py` + `test_kontroll_init.py`/`test_settings.py` extensions.

### feat(onboard): auto-derive the monitoring/logging FLOOR for a blind onboard (F2 de-bespoke, north-star Rung 0b) (2026-06-30)
A galaxy-onboarded device used to land on a generic backend role with **empty** `metrics:`/`logs:` — a managed but
UNMONITORED device (blank Grafana, no Loki stream). F2 closes that: a blind onboard now AUTO-DERIVES the universal
agent-less floor — snmp + blackbox metrics, syslog logs — from the same `predicate.classify` → `backend` seam that
already drives actuation/backup, so an arbitrary device reaches the SAME monitoring the ~7 curated classes get with
ZERO curation. The host→capability mapping lives in ONE place: each `ansible/backends/<name>/backend.yml` gains
`confers.telemetry`/`confers.logs` (method NAMES this paradigm can derive), and a method opts in by publishing a
`derive_default` (the universal param default) on its `telemetry/<m>.yml` / `logging/<m>.yml` descriptor — no
per-vendor branching in any generator. New `classify.derive_class_capabilities` (PURE/offline) emits the floor;
`build_onboard_plan` writes it into the new module marked `derived: true` (best-effort + NON-GATING, INVARIANT D*: a
derivation bug degrades to no floor, never a failed onboard) and carries an honest `capability_gap` naming the
vendor/credentialed richness it can't derive (surfaced advisory in the GUI, like provisioning). **Never fakes
telemetry:** a method is auto-derivable only if it needs no PER-CLASS secret (MF-S5 — snmp reuses the SHARED v3
read-only `snmp_observability` domain; `pve`/`rest_pull` with per-class secrets are NEVER conferred). New offline
`scripts/gen-class-capabilities.py --check` (wired into `tests/validate`) keeps each derived class's floor honest
against the derivation (the no-bespoke teeth; churn → a reviewable diff, not silent rot). **cisco_ios migrated as
the frozen-oracle proof:** marked `derived: true` + a committed `facts.pinned.yml`; the generated Prometheus targets
+ Vector drop-in + backup schedule are **byte-identical** (the three generators read method/params only, ignoring
the marker — verified by all three `gen-* --check` staying green). Backup stays the existing role-conferred path
(not derived). proxmox/fortigate residual-isolation + the dashboard floor-board are follow-up rungs.

### feat(gui): render the derived per-backend credential fields in the onboard form (F1 TIER-A, north-star Rung 0a-2) (2026-06-30)
The GUI half of Rung 0a — the operator now SEES the right credential inputs for the device they picked. On the
onboard form's first open it fetches the new read-only **`GET /onboard/cred-fields?backend=…`** route (mirrored on
both the typed API and the in-process Flask GUI) and renders the derived `[CredField]` descriptors with
`renderCredFields`, **reusing the existing secret-form widget** (`renderSecretKnob`) — zero new renderer code. A
`network_cli` switch shows SSH login + the enable secret and **no API-token box**; a REST/`httpapi` device shows a
token and **no dead SSH-key box** (the operator's exact gap, now fixed end-to-end through the UI). The four static
credential inputs are gone; submit harvests every derived `[data-field]` into the request's **`creds` map**
(`OnboardIn.creds` / the GUI route both fold a legacy scalar caller into it for back-compat), and `onbPristine`'s
collapse gate is now a STRUCTURAL scan of `[data-field]` so a newly derived field can never escape it. The descriptors
are projected through `authspec.public_descriptor` so the wire surface stays **names-only** (no value ever rides a
plan view or the cred-fields read). New testids (`cred-fields`, `field-cred-<field>`, `cred-derivation-source`,
`cred-fallback-banner`) in `tests/testid_reference.md`; covering GUI/API route tests + an updated e2e proving the
switch derives SSH login and no token box in a real browser. A backend that declares no `auth:` block still falls
back to the generic union (a banner explains it) so onboarding is never blocked (INVARIANT D*).

### feat(onboard): self-describing credential fields, derived per-backend (F1 TIER-A, north-star Rung 0a) (2026-06-30)
The first build rung of the blind fresh-GUI, zero-bespoke onboarding north star
(design-of-record `docs/reviews/2026-06-29-north-star-onboard/`). The onboard form's credential fields are now
**derived from each backend's declared auth SHAPE** instead of a static one-size-fits-all 4-field union: a
`network_cli` device gets SSH login (+ an optional enable secret), a REST/`httpapi` device gets an API token and
**no dead SSH boxes** — the exact gap the operator hit onboarding the firewall (a generic `backend_api` class). New
`scripts/kontroll/authspec.py` reads a per-backend `auth:` block (config-as-data, in `ansible/backends/<name>/
backend.yml`) and emits `[CredField]` descriptors; `build_onboard_plan` replaces its former four-branch credential
if-ladder with **one data-driven loop** over those fields (a net deletion of branching — the host→auth mapping now
lives only at the backend-descriptor seam). A password/key/token onboard stays **byte-identical** (a back-compat
safety net maps any legacy scalar a caller still passes), proven by the unchanged onboard suite. The plan now returns
`cred_fields` (the descriptors — names/kinds only, **never values**) so the dry-run/GUI can show exactly which fields
a device needs. Security: a new **domain-confinement guard** (MF-S2) refuses, before any cred write, a derived secret
field stamped for a different SOPS domain than the onboard targets. This is the SERVER slice; the GUI rendering of the
derived fields (`renderCredFields` + the `GET /onboard/cred-fields` read route) follows in Rung 0a-2. TIER B (exact
fields from a module `argument_spec`) is a later rung. New `tests/unit/test_authspec.py` + onboard-test extensions.

### fix(install): provision the C10 canonical group (gid 1001) on the host floor, not as a manual step (2026-06-29)
Live-caught on a baked prod box: the canonical `/srv/kontroll.git` is group-gid-1001 (the
uid:gid the privileged api/onboard-gui write proposals as), and the operator's `kontroll-promote` deletes the consumed
`proposed/<run_id>` ref — which fails **`Permission denied`** unless the operator is a member of that group.
`local-canonical.yml` provisions the named group + the operator's membership on a host-direct run, but **skips** them
inside the installer container (`when: not in_container`) — which is exactly how the published-kit (`kontroll.sh`) and
local-build (`kontroll-installer.sh`) launchers run the deploy, because a container-local group dies with the ephemeral
installer. So a published-kit install left the group unprovisioned and every promote after staging failed; the gesture
was only **printed** as a manual `sudo groupadd … && sudo usermod …` step the operator never ran. Fix: a shared,
idempotent **`scripts/lib/canonical-group.sh`** (POSIX sh) with `canonical_group_{satisfied,ensure,ensure_or_warn}`.
`install-prereqs.sh` (root) now **creates** the group + adds the operator (parallel to the existing docker-group
gesture); both launchers **ensure-or-warn** at run time — auto-provision via `sudo -n` if available, else print the
exact one-liner loudly (never silent, never fatal). The printed manual step is removed from `install-prereqs.sh` /
`install.sh` / `docs/install-from-scratch.md`. Pinned by `tests/unit/test_canonical_group_provisioning.py`
(helper verbs + each call-site + the manual step stays gone). SECURITY.md C10.

### fix(gitio): rebase a staged proposal onto current canonical main so back-to-back promotes stay fast-forward (2026-06-29)
Live-caught on a baked prod box: the onboard-GUI/API content clone commits each
`proposed/<run_id>` on its **local** `main`, which `_reset_content_clone_to_canonical` last reset to `local/main`
*after the previous stage* — i.e. **before** that proposal was promoted. So once the operator promoted proposal A
(canonical `main` → `seed+A`), the next stage B committed on the stale pre-A base and pushed `proposed/B = seed+B`,
which is **not** a fast-forward of the advanced canonical `main` (`= seed+A`). Every promote after the first then
failed *"not a fast-forward; re-propose against current main"*, forcing a manual
`docker exec onboard-gui … git fetch -q local && git reset -q --hard local/main` between promotes. Fix: in staging
mode `commit_and_push` now calls `_rebase_proposal_onto_canonical` (a `git fetch local` + `git rebase local/main`)
**before** the canonical push, re-basing the single proposal commit onto current main so disjoint proposals (the
common case — onboard device A then device B = different inventory files) become a clean FF with no manual re-sync.
The honest boundary: a **genuine** conflict (the proposal and the promoted change touch the same lines) aborts the
rebase and pushes the proposal as-is, so `promote_ref`'s FF-gate still correctly refuses it (re-propose) — never a
silent auto-merge that could clobber the promoted change. Registered in the P0b read-only write-verb pin and guarded
by two `tests/unit/test_staging_isolation.py` cases (disjoint→FF, conflict→left-non-FF). C10 unchanged: the GUI still
only stages; the trusted second key still promotes.

### release(images): pin the v0.1.1 prod image digests (the keygen-fix release) (2026-06-29)
Re-pin `docker/images.lock.yml` to the `v0.1.1` published digests — the release carrying the `fix(keygen)` overlay-path
fix above, cut so the prod migration's baked prod box can pick it up (the box runs the baked code from the
digest-pinned control image, so a code fix needs a republished image). `kontroll-control` →
`sha256:fb6e2107…`, `kontroll-installer` → `sha256:784d43c5…`, `kontroll-vector` → `sha256:59516ae7…`. Same
deliberate-release-tag discipline as v0.1.0 (never the mutable `edge`).

### fix(keygen): stage the recipient at the OVERLAY-resolved path (instance/.sops.yaml), not the bare key (2026-06-29)
Live-caught during the prod migration's first real GUI keygen (break-glass). `apply_keygen_plan` reported its
commit path as the bare `".sops.yaml"`, but the recipient is written to `paths.resolve(".sops.yaml")` — which the
instance overlay maps to `instance/.sops.yaml` on every real deploy. So the route's `git add .sops.yaml` (from the
clone root) matched nothing → the keygen commit failed → the minted public recipient **never staged** (the private
key was shown once and stored, but its recipient was lost — a silent crown-jewel-recovery gap). Latent because GUI
keygen had never run on a real overlay box; the unit fixture was flat (`tmp/.sops.yaml`), where `overlay_rel` is the
identity, so it didn't exercise the bug. Fix: report `paths.overlay_rel(".sops.yaml")` (→ `instance/.sops.yaml` when
the overlay is active, `.sops.yaml` legacy) — exactly how the working onboard/secrets paths report theirs. The other
mutators (secrets/actuation/settings/hostvars/observe/logsvc) already use `overlay_rel`/`overlay_target` correctly;
keygen was the lone offender. Guarded by a new overlay-fixture regression test asserting the reported path resolves
to the file actually written (`tests/unit/test_keygen_service.py`). Requires a republished image to reach a baked box.

### release(images): pin the v0.1.0 prod image digests (2026-06-29)
The first real digests committed to `docker/images.lock.yml` (it was all-`null` — local-build fallback only). Cut for
the Phase-B prod cutover (design-of-record `docs/reviews/2026-06-29-migration-scope/`): the `v0.1.0` tag
triggered `publish-images` (gate → control/vector/installer matrix), and `gen-image-digests.py --refresh v0.1.0`
recorded the published `@sha256` digests — `kontroll-control` (the code-baked runner), `kontroll-installer`,
`kontroll-vector`. Under `use_published_images=true` the deploy now pulls these by digest (Docker-verified on pull —
the image-world L2b); the launch kit's `images.env` renders from this lock. Riding a release `v*` tag, never the
mutable `edge` (R-DIGEST). cosign signing stays deferred (honest-amber provenance).

### feat(docker): baked-code manifest — the "L2b for code" recorded floor (Phase B M6) (2026-06-29)
Phase B bakes the importable package `scripts/kontroll/` into the published, world-pullable control image at
`/opt/kontroll`, so the network services run their actuation code from the digest-pinned image. The image digest
already binds the whole image's bytes and **M8a** byte-identity-checks the top-level promote slice; **M6** adds the
missing middle layer — a committed, per-file sha256 **manifest** of the baked package (`docker/code-manifest.lock.yml`,
generated by `scripts/gen-code-manifest.py`). It is the code-world analogue of L2b's recorded-sha256 floor for
collection tarballs: (1) a reviewable in-git record of exactly which code bytes ride the image; (2) a `--check`
staleness gate wired into `validate` **and** the publish-images `gate` job (so a tag can never publish baked code that
drifts from the committed, reviewed manifest — M5-adjacent); (3) a `gen-code-manifest.py --verify <tree>` verb an
operator/audit runs against a materialized baked tree (`/opt/kontroll`) to prove the running code matches the recorded
manifest, fail-closed on any added/removed/changed file. All modes are OFFLINE (pure file hashing — the BRICK-1
discipline). Closes the last non-costed Phase-B SHOULD-tier hardening item (the design-of-record's M6; M7/M10 already
landed). Covered by `tests/unit/test_code_manifest.py`; `SECURITY.md` C16; `docs/reviews/2026-06-29-phase-b-baked-code/`.

### fix(gui): baked capability-promote regen writes to write_root(), not the read-only baked root (2026-06-29)
A Phase-B bug found during the Rung-3 closure analysis: when a service runs BAKED, `service/observe.py`,
`logsvc.py`, and `backup.py` load the config-as-data generator from `/opt/kontroll` (the baked tree), so
`gen.ROOT == paths.ROOT`, the self-protect guard passes, and `gen.main()` wrote `gen.ROOT/{prometheus,docker,config}`
— which is `chmod -R a-w` read-only (the Rung-1a bake). So a capability-promote (telemetry / logging / backup
schedules) **failed** under baked code. Fix: after the guard, repoint `gen.ROOT = paths.write_root()` so the
regenerator reads the operator's proposed state + writes its targets in the `/propose` clone. Identity when
`write_root()==ROOT` — the legacy `/repo` deploy + deploy-stack (which run the generators directly with `ROOT` already
the writable clone) are byte-for-byte unchanged. Not caught by the Rung-2 onboard→promote dogfood (which never
exercised capability-promote). Covered by `tests/unit/test_baked_regen_write_root.py` (all three regenerators ×
baked-targets-write_root + unset-is-identity).

### feat(install): shrink the launch kit / canonical to drop the baked code (Phase B Rung 3) (2026-06-29)
With the api/onboard-gui services running baked from the published control image (Rungs 1–2), the launch kit (which
seeds the C10 canonical) no longer needs the **baked code**. `make-launch-kit.sh` now strips `api/ gui/ tests/
.github/ .claude/` (the kit was already stripping `instance/` + `docs/reviews`), and the `kontroll` launcher forces
**`bake_code=true`** alongside `use_published_images=true` for `init`/`check` (a minimized kit has no `api/gui` source
to local-build, so the services MUST run baked). A read-only **closure analysis** (adversarially verified) drove the
exact strip and **corrected the design-of-record**: the synthesis's "4-file promote slice" was under-counted — because
the kit *is* the canonical seed *is* deploy-stack's runtime tree, the canonical must keep the seven deploy-stack
`gen-*.py` + their `kontroll/` import closure, AND **`gen-requirements.py`** (the FIX-M9 pin-conflict gate execs the
*proposed tree's own* copy of it — absent it the never-brick gate **fails open**). So this shrink is **conservative**:
it drops only the adversarially-verified-safe baked bulk (no canonical/install/promote/deploy-stack reader; services
run baked) and keeps `scripts/` WHOLE — zero closure-miss risk; the finer `scripts/`-level strip is a future refinement.
**M8a** (Fork-A MUST): the promote slice (`kontroll/{__init__,gitio,paths}.py` + `kontroll-promote.py` +
`gen-requirements.py`) is baked into the runner image **and** kept in the kit — byte-identical by construction, so the
trusted `kontroll-promote` FF-gate is never forked from the baked code. Covered by `tests/unit/test_launch_kit.py`
(strip + launcher-force + M8a + the live-assembly closure check). SECURITY.md C16/M7 records the residual strengthen.
Design-of-record: `docs/reviews/2026-06-29-phase-b-baked-code/99-synthesis.md` §1 (Rung-3 closure correction) / §5. A
separately-tracked latent bug was found (baked capability-promote regen writes the read-only `/opt/kontroll` — must
target `write_root()`), out of the shrink scope. **Verified by a minimized-kit fresh-VM dogfood (published+baked).**

### feat(install): make the host floor nixflavor-agnostic (package-manager dispatch) (2026-06-29)
The Rung-2 dogfood (a real fresh-node install) confirmed the install path's one non-portable surface: the host-floor
scripts (`scripts/install-prereqs.sh`, `scripts/host-systemd.sh`) were hard-wired to **apt**, blocking a
Fedora/RHEL/Rocky/Arch/openSUSE operator at step one. New `scripts/lib/pkg.sh` (a sourced helper) detects the package
manager (**apt/dnf/zypper/pacman**) + init system and dispatches `pkg_install`/`pkg_refresh`/`svc_enable_now`/
`arch_deb`. `install-prereqs.sh` now installs the floor (`git tar curl`) via the dispatch, keeps the **apt deb822
Docker repo** path (which must stay byte-matched to `docker/installer/Dockerfile` + `deploy-stack.yml`) on the apt
branch, and uses Docker's official **cross-distro installer** (`get.docker.com`) on dnf/zypper/pacman; it enables
Docker via `svc_enable_now` (systemd/OpenRC-gated, no longer a bare `systemctl`) and **verifies `docker info`** so a
non-systemd host fails loud, not silent. The legacy `--with-host-ansible` path derives the sops arch from `uname -m`
(not `dpkg --print-architecture`) and tries `pip install` before falling back to `--break-system-packages`.
`bootstrap.yml`'s host-direct sops download is arch-mapped (was a hardcoded `.linux.amd64` — a latent aarch64
blocker). The architecture already made this easy: the compose-native design pushes ansible/sops/age/collections into
the installer **image** (intentionally Debian internally — untouched), so only the host *floor* was ever
distro-coupled. **Not install-gating / BRICK-1-neutral** (runs before the offline never-brick generators, network
allowed). Covered by `tests/unit/test_install_portability.py`; the apt branch was re-verified live + the
dnf/zypper/pacman branches proven via throwaway distro containers on the dogfood VM. Doc signposts in `SETUP.md` +
`install-from-scratch.md`. Source audit: a read-only portability sweep of the documented install surface.

### feat(gui): flip onboard-gui to baked code + relocate its audit log (Phase B Rung 2) (2026-06-29)
With `-e bake_code=true` the **onboard-gui** service now runs `gui/app.py` from the baked control image at
`/opt/kontroll` (`working_dir`/`ANSIBLE_ROLES_PATH` = `KONTROLL_CODE_ROOT`) and writes the C10 propose tree at
`/propose` (`KONTROLL_WRITE_ROOT`); the `${…:-/repo}` defaults keep an un-baked deploy byte-identical. onboard-gui is
the higher-privilege flip (it holds the age key + the widest propose surface: onboard/secrets/keygen/actuation/
reconfigure/regen), so three Rung-2-specific seams land with it. **M11 — audit relocation:** the GUI audit log moves
off `/repo/local/onboard-gui-audit.log` (inside the read-only-when-baked content clone, wiped by its `reset --hard`)
to a dedicated `/audit` bind (`KONTROLL_GUI_AUDIT_DIR`, deploy-stack-provisioned uid-1001/0755, mirroring the api
audit dir); the Vector tail follows it (the `/host/gui-audit` source moves off `…/onboard-gui/repo/local`). **Fork B —
the `modules/` overlay:** `modules/` is the one registry that grows with operator action (onboarding) and is edited in
place (reconfigure), so the runtime service readers now resolve it through `paths.module_file()`/`module_keys()` (the
write_root clone SHADOWS the baked ROOT — an operator-onboarded or reconfigured class is read from the propose clone, a
pristine shipped class from the image). **DECISION D1** (ratified by the catalog-reader trace over D3's lean): operator
classes land in the canonical's `modules/`, so the deploy-time generators (incl. the offline BRICK-1 `gen-requirements`)
keep reading `modules/` with ZERO change — the overlay is confined to the baked runtime readers. **Config-as-data:** the
reads Rung 0 left on `paths.ROOT` for `config/` (capture-redactions), `dashboards/` (Grafana deep-link uid), and the
trust/image locks (`provenance.py`) now use `write_root()` — those dirs are config-as-data in the clone, NOT baked; this
also fixes a latent Rung-1b regression where the baked api's provenance surface silently read empty. Only **semaphore**
stays on `/repo` (Rung 3, the promote-script seam). Covered by `test_bake_code_flip_gui.py` (flip + M11 + the storage
provisioner + the reserved-env pin) and `test_module_overlay.py` (the overlay semantics + the config-as-data routing);
`KONTROLL_GUI_AUDIT_DIR` added to `gen-secret-env.py`'s reserved set + the storage-chown `FACT_DEFAULT`. Design-of-record:
`docs/reviews/2026-06-29-phase-b-baked-code/99-synthesis.md` §2 (Fork B) / §3 (M11) / §5. **Next: the costed scratch-VM
dogfood (onboard-gui baked + the full onboard→stage→promote arc) — the §5.3 gate.**

### feat(api): flip the api service to run baked code, flag-gated (Phase B Rung 1b) (2026-06-29)
With `-e bake_code=true` (deploy-stack, default false), the **api** service now runs code from the baked control
image at `/opt/kontroll` (`working_dir`/`PYTHONPATH` = `KONTROLL_CODE_ROOT`) and writes the C10 propose tree at
`/propose` (`KONTROLL_WRITE_ROOT`, the `paths.write_root()` seam, Rung 0). deploy-stack renders the two code-root vars
into `docker/.env` only under `{% if bake_code %}`, so the default deploy emits neither and the compose
`${KONTROLL_CODE_ROOT:-/repo}` / `${KONTROLL_WRITE_ROOT:-/repo}` defaults keep the legacy `/repo`-clone path
byte-identical. The host content-clone dir stays `…/api/repo` (deploy-stack clones it unchanged); only the container
mount **target** is the write-root. Per-service + lowest-blast-radius first: **only api flips** — onboard-gui +
semaphore stay on `/repo` until their own rungs (audit repoint / `modules/` layout / the promote-script seam). The
api is read-only by default (no token ⇒ 503), holds no age key, runs no roles — so the armed propose path is the only
mutator the seam bites. MF-4 honored (the write-root rides the config-time `.env`, never a per-service `env_file` —
compose fixes the volume mode at config time). V1-§3: the installer keeps `KONTROLL_WRITE_ROOT` UNSET so the
deploy-time `paths.py resolve` CLI stays on its `/repo` canonical clone (deploy-read == the canonical `instance/secrets`
the baked GUI writes). SECURITY.md C10 records the M7 strengthen (the baked api imports code from the digest-pinned
image, not the `:rw` clone — an armed-RCE clone-write can no longer inject in-process code). Covered by
`test_bake_code_flip.py` (the api flip + default-legacy + only-api + the installer-WRITE_ROOT-unset guard);
`KONTROLL_{CODE,WRITE}_ROOT` added to `gen-secret-env.py`'s reserved platform-core env set. Design-of-record:
`docs/reviews/2026-06-29-phase-b-baked-code/99-synthesis.md` §5/§6. **Next: the costed fresh-VM dogfood (publish a
code-baked control image + an api-baked install round-trip) — the §8.3 gate.**

### feat(docker): bake the kontroll code into the control image (Phase B Rung 1a) (2026-06-29)
The control image (`docker/semaphore-runner/Dockerfile`) now bakes the IMMUTABLE read-root — `api/ gui/ scripts/
ansible/` + the data registries (`modules/ vectors/ overrides/ telemetry/ logging/ capabilities/ secret-forms/
key-roles/ settings/`) — at `/opt/kontroll` (`chmod -R a-w`), so a baked deploy (Rung 1b, the api fragment flip) runs
code from the image instead of a host `/repo` clone. `paths.ROOT` derives to `/opt/kontroll` automatically (three
dirnames up from the baked `scripts/kontroll/paths.py`); the mutable C10 propose tree stays a thin host clone via the
`paths.write_root()` seam. The bake is **inert until a service is flipped** — nothing references `/opt/kontroll` yet,
so behaviour is unchanged; the image is just larger. NEVER baked: `instance/` (the private overlay + secrets, stripped
by the repo-root `.dockerignore`) and the `*.generated.*`-bearing config-as-data dirs (`prometheus/ dashboards/
config/` — they rot / carry real topology). Two guards land with the bake: **M9** — `test_publish_images.py` pins that
the build context CONTAINS `api/ gui/ scripts/` (no silent un-bake) and EXCLUDES `instance/`/secrets/the generated
config dirs (no leak into the world-pullable image); **M5** — `publish-images.yml` gains a `gate` job (the
read-only-by-construction completeness pin + the WRITE_ROOT seam) that the `publish` job `needs:`, so a tag can never
publish code the AST pin hasn't cleared (baking moves the read-only guarantee off the running clone onto the
digest-pinned image — review 21 §3.3/§5.1). SECURITY.md C16 extended (the control image as a published code surface).
Design-of-record: `docs/reviews/2026-06-29-phase-b-baked-code/99-synthesis.md` §1/§6.

### refactor(scripts): paths.write_root() — the Phase-B baked-code WRITE-root seam (2026-06-29)
Phase B (make the stack services run BAKED code from the published control image instead of the `/repo` clone, so the
C10 canonical seeds from a minimal kit) reduces to one architectural lever: split `paths.ROOT` — today a single
`__file__`-derived constant that is the read-root (data registries), the write-root (the propose tree), and the git
cwd all at once — into a read-root (`ROOT`, the immutable code + registries, baked read-only) and a **write-root**
(`paths.write_root()`, the thin propose clone where every mutator writes + git runs). This rung lands the seam ONLY,
as a pure refactor with `write_root() == ROOT` the identity: with `KONTROLL_WRITE_ROOT` unset (the CLI, the tests, the
legacy `/repo`-clone deploy) behaviour is **byte-for-byte unchanged**. A future baked deploy sets
`KONTROLL_WRITE_ROOT=/propose` so the read-only image tree at `ROOT` is never written (`/opt/kontroll` is a `COPY`'d
tree with no `.git`, so git MUST run in the clone). Routes the mutator/git seam — `gitio._run` cwd +
`offsite_remote_exists`, `_write_new`, `write_inventory_host`, `sops_write_domain` (and `sops_set`/`keygen` via
`resolve`/`_sops_path`), plus `actuation`/`settings`/`hostvars`/`homepage`/`capture_exception`/`units` write +
paired existence-read sites — and the instance-overlay helpers (`resolve`/`overlay_target`/`overlay_rel`) to
`write_root()`; the data-registry reads (`modules/<key>` direct joins, the `*_DIR` constants) stay on `ROOT` (the two
seams are NON-overlapping — review 30 §2.3). The `modules/<key>` operator-class read split (shipped-at-ROOT vs
operator-onboarded-in-clone) is deferred to a later rung with a catalog-reader trace (it is identity here). No new
write verb (the read-only AST pin is unaffected). Design-of-record:
`docs/reviews/2026-06-29-phase-b-baked-code/99-synthesis.md`; covered by `test_write_root_seam.py` (identity +
env-override + the non-overlapping registry/overlay split + the armed-write-lands-in-clone proof).

### fix(install): C1 bundle-as-compose — five fresh-VM dogfood fixes (2026-06-29)
The deluxe C0+C1 verify on a FRESH VM (Docker+git only, install from a pinned launch kit pulling every image by digest)
surfaced five bugs the text-only tests missed — all fixed; the verify then PASSED end-to-end (10 containers on the ghcr
digests, host has no ansible/sops/age, GUI :8443 up):
- **`scripts/kontroll.sh` — drop the invalid `--no-build` flag.** `docker compose run --no-build` isn't a flag on all
  compose versions ("unknown flag"). The host pre-pull guarantees the image is present, so a plain `run --rm init` uses
  it without building (`run` builds only when the image is absent).
- **`scripts/kontroll.sh` — git-init the kit tree.** A launch kit is a tarball extract with no `.git`; local-canonical
  seeds the C10 canonical from the working tree's git, so the launcher now git-inits + commits it (the step
  `install.sh` does for the bundle path) — else `git remote add local` fails "not a git repository".
- **`scripts/kontroll.sh` — host pre-pull the published images.** deploy-stack's `docker compose up` runs inside the
  installer container (root, no ghcr creds) → an in-container pull 401s. The launcher now pulls every published image
  on the HOST (where the operator is `docker login`-ed) first, so compose-up finds them present (pull-only-if-missing).
- **`make-launch-kit.sh` + `kontroll.sh` — strip CRLF from images.env.** A Windows build host's `gen-image-digests.py
  --env` emits `\r\n`; the trailing `\r` yields "invalid reference format" on pull. make-launch-kit now `tr -d '\r'`s
  it and the launcher reads it CR-tolerantly.
- **`deploy-stack.yml` — `check_mode: false` on the digest-read.** The read is an `ansible.builtin.command` skipped
  under `--check`, so `kontroll check` fired the fail-closed "no recorded digest" assert. The read is offline +
  read-only, so it now runs under --check (pinned by the resolver guard in test_deploy_stack_check_mode.py).
Covered by `test_launch_kit.py` (the new launcher seams + the make-launch-kit/deploy-stack guards) + the extended
`test_deploy_stack_check_mode.py` resolver set.

### feat(install): C1 follow-on — air-gapped bundle (docker save) (2026-06-29)
`scripts/make-bundle-airgap.sh` (report 22 §6.3) builds a fully OFFLINE bundle for a control node with zero registry
reachability: it pulls every kontroll image BY DIGEST for the target arch, `docker save`s them into one tar, and
bundles the launch kit alongside. The digest-pin survives the save/load round-trip, so the loaded images are the exact
verified bytes — no Galaxy, no ghcr, no `docker build` on the air-gapped node. Consumer side: `docker load -i …` then
`./kontroll fresh-init …`. The `kontroll` launcher is now **air-gap-aware** — it skips the registry pull when the
installer image is already present locally (the loaded one), so the same launcher works online and off-grid. Requires
a digest-pinned `docker/images.lock.yml` (refuses an unpinned air-gap bundle). Covered by `test_launch_kit.py` (the
builder's pull-by-digest/save/kit contract + the launcher's skip-pull-when-present tolerance).

### feat(gui): C1 follow-on — the image-provenance surface (2026-06-29)
Extends the MF-5 supply-chain honesty to the IMAGE chain (report 22 §7.3). `service/provenance.image_provenance()`
projects `docker/images.lock.yml` into one honest class per kontroll image — `digest-pinned` (amber: a recorded
`@sha256:` Docker verifies on every pull, **not** a signature), `local-build` (no digest ⇒ built on the node), or
`signed` (green — cosign, the deferred rung, never true today). `GET /api/image-provenance` (read-only,
`assert_read_only`-pinned, reads only image names + public digests — no secret) powers an **images sub-panel** in the
Index "Supply chain" section, so a digest-pin is never mistaken for a verified signature. Covered unit + route +
testid + e2e (`image-provenance-*`). The honest framing mirrors the collection surface exactly: digest-pinned is real,
but it is not signature-verified, and the UI says so.

### feat(install): C1 — publish the installer image + bundle-as-compose (2026-06-29)
Extends C0 to the compose-native INSTALLER so a fresh control node can install from PUBLISHED, digest-pinned images
with **no git clone and no `docker build`** — the last build-on-the-control-node step is gone (report 22 §2.1 Image C
/ §6 distribution format). Opt-in + additive: the local-build launcher (`scripts/kontroll-installer.sh`) is unchanged.
- **The installer image is published + digest-pinned.** `kontroll-installer` joins the `publish-images.yml` matrix
  (built from `docker/installer/Dockerfile`; NOT the all-modules superset — it bakes only the control-plane base), the
  `gen-image-digests.py` `KONTROLL_IMAGES` map, and `docker/images.lock.yml`. `docker/services/installer.yaml`'s image
  ref becomes `${KONTROLL_INSTALLER_IMAGE:-kontroll/installer:latest}` — local build by default, the published digest
  when set. The SAME fragment serves both paths (`run --build` builds; `pull init` + `run --no-build` pulls the digest)
  — no second compose file, no duplication.
- **`scripts/kontroll.sh`** — the bundle-as-compose launcher (the published-path sibling of `kontroll-installer.sh`):
  sources the digest pins from `images.env`, pulls the installer by digest, runs it `--no-build`, and forces
  `use_published_images=true` for init/check so deploy-stack pulls the runner/vector by digest too. Host floor stays
  **Docker + git only** (no host python/ansible — the launcher reads pre-rendered pins).
- **`scripts/make-launch-kit.sh`** — assembles the launch kit: the instance-stripped tree (the `make-bundle.sh` strip
  — NO `instance/`, NO review snapshots, image-world #72 §7.2) + `images.env` (rendered digest pins) + the `kontroll`
  launcher. Refuses to build an unpinned kit unless `--allow-unpinned`.
Covered by `tests/unit/test_launch_kit.py` (the launcher's pull/--no-build/use_published seam; the builder's strip +
pin + launcher; a real-assembly leak check) + the extended `test_publish_images.py` (the installer across all four
publish seams). Deferred to follow-on rungs: the air-gap `docker save` tar, the GUI image-provenance panel, and full
launch-kit file-set minimization. The live "fresh node installs from the kit" is a separate dogfood.

### feat(install): C0 — publish the runner + vector images to private ghcr, pull by digest (2026-06-29)
The control node no longer has to BUILD the Semaphore runner + Vector images — they can be published once to PRIVATE
`ghcr.io/netcanon-dev/kontroll-{control,vector}` and pulled BY DIGEST. Opt-in + additive: the local `docker build`
path is unchanged and stays the default; `-e use_published_images=true` flips to digest pulls. The pieces:
- **`scripts/gen-requirements.py --all-modules`** — the offline SUPERSET lockfile: the union of EVERY public module's
  collections (a published image can't know a node's fleet, so it bakes the whole catalog; the per-node fleet then
  only SELECTS which baked collections a play uses). The published `kontroll-control` runner bakes this.
- **`scripts/gen-image-digests.py` + `docker/images.lock.yml`** — the image-world pin floor (the L1/L2b analogue):
  every kontroll image pinned by `@sha256:`, which Docker verifies on every pull. `--env` (OFFLINE) emits the
  `KONTROLL_{CONTROL,VECTOR}_IMAGE=ref@digest` vars the deploy pins into `.env`; `--refresh` (dev/CI, network)
  resolves a published `tag → digest`; `--check` (validate) is a schema/offline gate.
- **`.github/workflows/publish-images.yml`** — on a release tag `v*` (or `workflow_dispatch`), builds + pushes the
  two images to private ghcr with the built-in `GITHUB_TOKEN` (no new secret), records the digests. Cosign signing
  is the deferred rung (mirrors the never-brick keyring).
- **deploy-stack** gains `use_published_images` (default false): when on, the two `docker_image_build` tasks skip and
  `docker/.env` pins the digests from the lock (fail-closed if none recorded); the runner refs become
  `${KONTROLL_CONTROL_IMAGE:-kontroll/semaphore:latest}`. The control node does a one-time `docker login ghcr.io`
  (a read:packages PAT) to pull the private images.
Publish flow: tag → CI publishes → `docker login ghcr.io` + `gen-image-digests.py --refresh <tag>` → commit the lock
→ deploy `-e use_published_images=true`. Covered by `tests/unit/test_publish_images.py` (superset coverage, lock
well-formed + offline + round-trips, opt-in gating, the overridable runner ref) + a `gen-image-digests --check`
validate step. This is Phase C0 of the compose-native design (report 22); C1 (the installer image + bundle-as-compose)
is next.

### fix(install): compose-native installer dogfood — the Phase-1 fixes a fresh-VM install actually needs (2026-06-29)
A live dogfood of the Phase-1 installer on a fresh Debian-12 VM (only Docker + git) brought the full 7-container
stack up green — after six fixes the gate surfaced. All now part of the installer:
- **Runtime collection path** — `docker/services/installer.yaml` sets `ANSIBLE_COLLECTIONS_PATH=/usr/share/ansible/collections`
  (ansible.cfg's relative `collections_path` shadowed the baked control-plane collections → `check` failed
  "couldn't resolve community.docker.docker_image_build").
- **Same-path repo bind (the big one)** — `docker compose up` runs *inside* the installer but binds against the
  HOST daemon, so the stack's relative bind sources (e.g. prometheus `../../prometheus/prometheus.yml`) must
  resolve to real HOST paths. The repo is now bound at `${KONTROLL_REPO_ROOT}:${KONTROLL_REPO_ROOT}` (same path
  in-container as host), passed into the container env, and the entrypoint resolves the repo from it.
- **Image runtime deps** — the `docker` Python SDK (community.docker needs it; was "No module named 'requests'")
  and `openssh-client` (bootstrap mints the device key with `ssh-keygen`) added to the Dockerfile.
- **logrotate is host-systemd** — deploy-stack's logrotate apt + drop-in skip in-container (no `python3-apt` for
  check mode; a drop-in in the container's /etc never reaches the host) and move to `scripts/host-systemd.sh` (G5).
- **Launcher `--build`** — `scripts/kontroll-installer.sh` rebuilds so a bundle update that changes the Dockerfile
  is reflected (a plain `run` would silently use stale glue).
Gate (all PASS): 7 containers up; 2nd run does not recreate them; `docker/.env` 0600 operator-owned; canonical
`operator:1001 2775` + denyNonFastForwards; the control age key is in NO image layer; **`which ansible` is empty on
the host while the stack runs**; the GUI serves HTTPS :8443 (401 Basic-auth). The G9 guard fail-closed when the
onboard-gui age key was absent (provisioned as the documented host gesture). NOTE: on a truly fresh node, run
`init` before `check` — `--check` skips the canonical's git-init command, so the dry-run can't preview the very
first install (it works for every subsequent change). Covered by `tests/unit/test_installer_phase1.py`.

### feat(install): compose-native installer — the host floor is Docker + git (Phase 1) (2026-06-29)
The install GLUE (a host ansible-core + the device collections + sops + age) was the deployability pain: a fresh
control node needed an exact host toolchain, and a version skew there was the entire fix(install) bug class below.
Phase 1 of the compose-native design (docs/reviews/2026-06-29-compose-native-install/) relocates only the install
*compute* into an **ephemeral, CLI-only, portless installer container** that bind-mounts the host's crown jewels
(age key, the C10 canonical, the rendered `.env` 0600 — host binds, never named volumes / image layers) and runs
the **unchanged** bootstrap / local-canonical / deploy-stack playbooks. The new operator flow needs only Docker +
git on the host:
```
sudo bash scripts/install-prereqs.sh                                  # Docker + compose + git (the whole host floor)
sudo groupadd -g 1001 kontroll && sudo usermod -aG kontroll "$USER"   # the C10 canonical group (one-time)
scripts/kontroll-installer.sh fresh-init --mgmt-ip <ip> --domain <domain>
scripts/kontroll-installer.sh check          # the mandated --check --diff dry-run
scripts/kontroll-installer.sh                # bootstrap → deploy-stack (the stack comes up)
scripts/kontroll-installer.sh configure-semaphore
```
- New `docker/installer/Dockerfile` (debian:12-slim + pinned ansible-core + the control-plane collections via the
  unchanged never-brick wrapper + sops/age + the docker CLI — **bakes NO secret, NO age key, NO instance/**; a
  repo-root `.dockerignore` is the guard) + `docker/installer/entrypoint.sh` (a thin init/check/fresh-init/
  configure-semaphore/shell/passthrough dispatcher) + `docker/services/installer.yaml` (the ephemeral, portless,
  `network_mode: host` service) + `scripts/kontroll-installer.sh` (the launcher that injects the operator identity).
- The playbooks gained a small, mechanical in-container parameterization: operator identity comes from
  `KONTROLL_OPERATOR_{USER,UID,GID,HOME}` (MF-1 — so a root-in-container run still chowns the canonical + renders
  `.env` to the real operator, and a non-root `compose up` can read it), and host-only tasks (the Docker install,
  qemu-guest-agent's systemd unit, the named gid-1001 group) skip when `KONTROLL_IN_CONTAINER=1` (MF-6). On a
  host-direct run those env vars are absent ⇒ byte-identical to before.
- `scripts/install-prereqs.sh` shrunk to the Docker + git floor; the legacy host toolchain (ansible-core + sops +
  age) stays behind `--with-host-ansible` for rollback. The host-systemd residual (qemu-guest-agent) moved to a
  new `scripts/host-systemd.sh`.
This structurally eliminates the fix(install) host-version bug class (there is no host ansible/sops/age to skew).
Covered by `tests/unit/test_installer_phase1.py` (no secret in an image/named-volume, ephemeral+portless,
SOPS_AGE_KEY_FILE not the literal env, no build-time keygen, the .dockerignore guard, the control-plane base covers
the install playbooks, the MF-1/MF-6 parameterizations, the entrypoint verb contract). Phase 1 = build + the
fresh-VM dogfood gate; published images (C) + the compose-native init-service decomposition (B) are later phases.

### fix(install): the fresh-Debian-12 install path runs clean — no manual ansible/collection surgery (2026-06-26)
A live dogfood of the documented from-scratch install (docs/SETUP.md → docs/install-from-scratch.md) on a clean
Debian 12 control node hit four ansible/collection version breaks that each halted deploy-stack or configure-semaphore.
All four fixed so the documented flow (install-prereqs → install.sh → kontroll-init --fresh → bootstrap →
local-canonical → deploy-stack → configure-semaphore) reaches a fully-up stack with NO ad-hoc surgery:
- **`install-prereqs.sh` installs `ansible-core` (>=2.16) via pip, not Debian's apt `ansible`** (core 2.14): the apt
  engine is too old for deploy-stack's `ansible.builtin.deb822_repository` (added in core 2.15), and its bundled
  community.* collections in dist-packages SHADOW the project lockfile pins. A pip core carries no bundled collections,
  so the lockfile installs cleanly with nothing to shadow it.
- **`scripts/install-collections.py` `version_satisfied` now honours a `>=` floor** (was: any floor treated as
  already-met): the distro-preseeded `community.docker 3.4.7` no longer masks the `>=4.0.0` pin, so the wrapper
  reinstalls the pinned >=4.0 into the project collections path where the deploy looks (else `docker_image_build`,
  added in community.docker 3.6, is unresolved at deploy-stack.yml:563).
- **`ansible/ansible.cfg` uses the built-in `default` stdout_callback + `result_format = yaml`** — the old bare
  `yaml` alias was `community.general.yaml`, REMOVED in community.general 12.0, which aborts every run on a current
  collection set ("community.general.yaml has been removed"). Propagates to Semaphore (same canonical ansible.cfg).
- **`scripts/configure-semaphore.py` self-decrypts `SEMAPHORE_ADMIN_PASSWORD`** from `instance/secrets/semaphore.sops.yml`
  when it isn't in the env (value never logged) — the documented `python3 scripts/configure-semaphore.py` now works on
  the control node with no undocumented export.
Covered by `tests/unit/test_install_collections.py` (the `>=` floor + the 3.4.7-vs-`>=4.0.0` shadow regression),
`tests/unit/test_configure_semaphore.py` (self-decrypt fallback / env-wins / file-absent), and a new
`tests/unit/test_fresh_install_compat.py` (install-prereqs pip-ansible-core + no apt `ansible`; ansible.cfg callback).
docs/install-from-scratch.md synced. Found + each root-caused by the live dogfood (worked around there to bring the
GUI up; this lands the durable fixes).

### feat(gui): MF-5 supply-chain provenance surface — no verification theatre (2026-06-25)
The operator-facing half of "a green 'installed' must never imply a signature was checked." The install wrapper
already prints an honest provenance summary at install time (NB-4); this adds the **persistent** surface so the
posture is visible whenever the operator reviews the fleet. New read-only `service/provenance.fleet_provenance`
projects the generated trust sidecar + the L2b digest lock into a per-collection verification class —
**`unsigned-pinned`** (amber — verified by the `==`/floor pin + sha256 checksum ONLY, no signature; the current
0-signature fleet) or **`signed`** (green — policy `required` AND a keyring held) — plus `digest_recorded` (L2b TOFU
bound) and a counts summary. `gui/app.py` `api_provenance` (`GET /api/provenance`, read-only `assert_read_only`-pinned,
audited `provenance-index` counts-by-class, `available:false` at HTTP 200 when the sidecar isn't generated yet);
the Index panel grows a fourth **Supply chain** section rendering each collection with an **amber** `unsigned-pinned`
badge + the honest note. Reads NO value/secret (the sidecar carries names + policy + a sha256 anchor only; SEC-2);
never gates onboarding (INVARIANT D*). Covered by `tests/unit/test_provenance.py` (all-unsigned-pinned on the
0-sig fleet, signed only when required+keyring, `digest_recorded` from the lock, degrade-when-absent, read-only pin)
+ `tests/integration/test_gui_api.py` (read + audit + available:false) + `tests/e2e/test_provenance_flow.py` (the
amber badge renders in a real browser). SECURITY C15-a + gui/README + testid_reference synced.

### feat(supply-chain): FIX-M9 — refuse a promote that would brick the next deploy's generate (2026-06-25)
A pin-conflict gate at the promote seam. Two app-store units pinning one collection to different exact `==` versions
both becoming `active` makes `gen-requirements` fail closed at GENERATE time — which halts the *next* deploy's
pre-image-build, a brick one seam removed from the cause (the unit that introduced it promoted cleanly; an unrelated
later deploy breaks). `gen-requirements.py` gains a **`--conflict-check`** mode (run the resolution — `resolve_pins`
fails closed on conflicting `==` — but render/write nothing); `kontroll-promote.py` now, before the fast-forward,
extracts the prospective post-merge tree (`proposed/<run_id>`, which a FF makes `main`) and runs **its own**
`gen-requirements --conflict-check` — refusing the promote (exit 2) if generation would fail closed, so two
conflicting `==` units can **never both become active**. `--skip-conflict-check` is the documented emergency
override. Read-only (extracts to a temp dir, never touches `main`); fails **open** on its own read error (git/tar
missing, an old/unreadable ref) — the generate-time fail-closed remains the backstop, so the gate hardens but never
blocks a clean promote. Covered by `tests/unit/test_kontroll_promote.py` (refuse/proceed/skip wiring + a real
git+subprocess integration test that detects a live conflict and returns clean on a single-unit tree) +
`tests/unit/test_actuation_registry.py` (the `--conflict-check` mode is resolve-only). SECURITY C10 promote rider.

### feat(supply-chain): never-brick L2b — the install wrapper records + re-verifies the artifact sha256 (2026-06-25)
The recorded-sha256 floor (L2b) that is the *entire* substitute for the absent signature on a 0-signature fleet
(`docs/reviews/2026-06-25-never-brick-supply-chain/`, Decision R1 / MF-1; previously deferred — the sidecar shipped
`sha256: null` and nothing bound it). `scripts/install-collections.py` now, **on the control node** (where its
persistent lock can live — `instance/trust/`): diffs the lockfile against what is installed
(`collection list --format json`, local — so a present pinned set does **no** fetch: air-gap-safe + idempotent),
pre-fetches only the to-install subset over cert-validated HTTPS, computes each tarball's `sha256` and TOFU-binds
it in a gitignored per-node lock (`instance/trust/observed-digests.yml`) keyed by exact `name==version` — **first
install records, a later mismatch fails closed without installing** — then installs `--offline` from the verified
files so the bytes verified ARE the bytes installed (MF-2). The runner image (I-2, no `instance/`) falls back to the
plain `-r` install unchanged; the gate is loud. `instance/trust/keys/.gitkeep` (+ the `instance.example/` template)
keeps the trust dir present (FIX-KEYS); the lock + any keyring are gitignored. Closes whole-artifact swap of a
recorded pin; version-bump on a `>=` floor stays the accepted R-NB-1 residual (closed only by an `==` pin) — SECURITY
C15-a worded to that. The generator stays **offline** (BRICK-1 — no network GET in an install-gating generator).
Keyring auto-provisioning (so a *signed* collection verifies) remains inert until a signing-enabled source exists.
Covered by `tests/unit/test_install_collections.py` (the present-set re-run does no fetch; a fresh install records
then installs `--offline`; a recorded-digest mismatch fails closed without installing; the diff/TOFU/parse pure
functions). **Dogfood-validated live** (clean ansible-core container, the control node, 2026-06-25): **D2** — a fresh
`ansible.posix` download → sha256 recorded → installed `--offline` (rc 0); **idempotent re-run** — present set, no
fetch, no `Installing` (rc 0); **D4** — a tampered recorded digest → `REFUSED … sha256 mismatch … Nothing installed`
(rc 3), collection absent. The dogfood **corrected the install step** to install the verified tarballs **by absolute
path** (the `ansible-galaxy collection download` requirements file references the tarball by a path relative to the
download dir, unreachable from `cwd=ANSIBLE_DIR`) — caught because the unit tests mock the subprocess.

### feat(gui): app-store configure dialog — the front-end chrome for an existing unit (2026-06-25)
The visible post-create **configure** flow, mounting the configure routes. The Index panel grows a third
**Automations** section (`GET /api/actuation`) listing the *registered* units (derived target / `==` pin /
knob-count / configured — NAMES only); each row's **Configure** opens `openUnitConfigDialog(key)`: the curated knob
group renders through the **same** `/api/configurable?kind=actuation-unit` projection + the **same**
`renderKnobGroup` (the `unit` source_kind — **zero new renderer code**) → **Preview** (`POST /api/actuation/<key>`
`apply:false`) renders the would-run play + the derived target + the `--check`-first hand-off, **writing nothing**
→ **Stage** (`apply:true`) stages `instance/actuation/<key>/vars.yml` to `proposed/<run_id>`; the result
(`unit-config-result`) reuses **`renderCapPromote`** verbatim. A knob edit invalidates the preview+token (re-Preview
required, the anti-drift gate). A **zero-knob** unit shows an honest `unit-config-noknobs` note and still
previews/stages. The GUI **never** promotes (no promote button — C10 two-key); the unit key is data on the dialog.
Covered by `tests/e2e/test_unit_config_flow.py` (Automations row → Configure → knob render → Preview play, modal
lifecycle); the configure dialog's `data-testid`s added to `tests/testid_reference.md`. This **completes the
post-create configure flow** (the app-store: search → create → configure → preview → stage). **Next foci** (confirm
first): the inert install rungs (keyring/L2b, only with a signing source) / backlog #72.

### feat(gui): app-store configure routes — the GUI mounts preview/stage of an existing unit in-process (2026-06-25)
The GUI half of the post-create **configure** flow (the front-end dialog + Index Automations section is the
follow-on rung). Two new routes on `gui/app.py` mirror `api/routes/actuation.py::stage` in-process, the same way
every GUI write dialog mirrors its API route: `GET /api/actuation` (the **Automations index** — the *registered*
units over `service/units.registered_units_view`, each unit's derived target/`==`-pin/knob-count/configured flag,
read-only + `assert_read_only`-pinned, audited `unit-registry`) and `POST /api/actuation/<key>` (the configure
write — preview `apply:false` → the would-run play + resolved non-secret vars + a token, writing nothing; stage
`apply:true` → write `instance/actuation/<key>/vars.yml` and STAGE it to `proposed/<run_id>` via
`commit_and_push(run_id)`, C10 never main). Validate-before-stage (422), the anti-drift token (409), and the
no-clobber/no-secret invariants hold at the GUI surface; the `unit-config-stage`/`unit-config-result` audit carries
names/counts/run_id only (no value is a secret — SEC-2); the GUI **never** promotes (C10 two-key — no promote
button). The curated knob group is the **same** read-only `/api/configurable?kind=actuation-unit` projection (no
new read). New `service/units.registered_units_view` + `_has_values` (read-only, pinned). Covered by
`tests/integration/test_gui_api.py` + the read-only pins (`test_units.py`, `test_actuation_service.py`). SECURITY.md
C10 STAGE rider + gui/README updated. **Next rung:** the front-end configure dialog + the Index Automations section.

### feat(gui): app-store create-unit dialog — the front-end chrome (2026-06-25)
The visible app-store flow, mounting the create-unit routes. Each search result card grows a **⚙ Automations**
badge (`unit-badge`) that opens `openUnitDialog`: pick a runnable unit (`unit-list`/`unit-row` from
`GET /api/units/<collection>`) → **Review** (a propose that renders the derived `device-class`/`inventory_group`,
the exact `==` pin, the `signature: adaptive` provenance, and the staged path — writing nothing) → **acknowledge**
the content-trust gate (`unit-blast-ack`, which gates Create) → **Create** (`POST /api/actuation/create` apply,
staging the descriptor to `proposed/<run_id>`). The create-time **Tier-cap** surfaces as a loud refusal pane
(`unit-tier-cap`) for an unsigned install onto `edge_firewall`/`core_switch` — no Create offered. Reuses the modal
lifecycle + `renderCapPromote` (the staged-ref + run_id result renderer) verbatim; the GUI **never** promotes (no
promote button — C10 two-key). Zero-knob MVP (the configure-knob pane that mounts `unit-config-fields` on an
existing unit is a further rung). Covered by `tests/e2e/test_unit_flow.py` (badge→dialog→list, lifecycle, the
result-renderer reuse via `page.evaluate`); ~20 new `data-testid`s in `tests/testid_reference.md`. **Next rung:**
the post-create configure flow (R3 knobs) on an existing unit.

### feat(gui): app-store create-unit routes — the GUI mounts the create flow in-process (2026-06-25)
The GUI half of the app-store create-unit stage (the front-end dialog is the follow-on rung). Two new routes on
`gui/app.py` mirror `api/routes/actuation.py` in-process, the same way every GUI write dialog mirrors its API
route: `GET /api/units/<collection>` (the Pick stage — a collection's runnable units over `service_units`,
read-only + `assert_read_only`-pinned, audited `unit-list`) and `POST /api/actuation/create` (the create write —
propose `apply:false` → the derived key + a token; create `apply:true` → render+validate the descriptor and STAGE
`instance/actuation/<key>/unit.yml` to `proposed/<run_id>` via `commit_and_push(run_id)`, C10 never main). The
create-time **Tier-cap** (403 for an unsigned install onto `edge_firewall`/`core_switch`), the collision guard
(409), the anti-drift token (409), and validate-before-stage all hold at the GUI surface; the audit carries
names/counts/run_id only (no create input is a secret); the GUI **never** promotes (C10 two-key — no promote
button). Covered by `tests/integration/test_gui_api.py` + the `api_units` read-only pin. SECURITY.md C10 create
rider + gui/README + logging-architecture audit rows updated. **Next rung:** the front-end dialog chrome
(badge → pick → review → create).

### feat(actuation): app-store create-unit — author an actuation unit from a searched collection (2026-06-25)
Fills the gap that blocked the app-store dialog: the R3/R4/R5 stages (configure/preview/stage) all read an
EXISTING `instance/actuation/<key>/unit.yml`, but nothing AUTHORED that descriptor — the registry was empty. New
`build_create_unit_plan`/`apply_create_unit`/`stage_create_unit` (`service/actuation.py`) + the
`POST /actuation/create` route render a unit descriptor from `{collection, kind, name, version, blast_radius}`
(the search + unit-enumeration outputs) and stage it on the SAME C10 two-key spine (`proposed/<run_id>`, never
`main`, never `promote_ref`). The descriptor's `device_class`/`inventory_group` are **derived from the declaring
module** (`catalog.module_for_collection` — the one dispatch seam), the version is an **exact `==` pin**, and
provenance defaults to `signature: adaptive` (the brick-proof default). **Zero-knob MVP:** the rendered descriptor
carries no `knobs:` block (the worked-extraction mandate blesses a knob-less unit); curated-knob authoring is a
later additive surface. Three guards are load-bearing: (1) **validate-before-stage** — the rendered descriptor
must pass the SAME `_actuation_schema.validate_unit` the generator enforces before it stages; (2) the **create-time
Tier-cap** — a collection whose class targets `edge_firewall`/`core_switch` is **refused (403)**, since an
unsigned public-Galaxy install may not reach the highest-blast tier (never-brick ∧ blast-radius); (3)
**collision-safe** — never clobber an existing unit (409). The descriptor schema + validator were extracted to
`kontroll/service/_actuation_schema.py` (the ONE contract `gen-actuation.py` and the create seam share). No secret
rides (the `actuation-create` audit is names/counts/run_id only). Covered by `tests/unit/test_create_unit_service.py`
+ `tests/integration/test_api_create_unit.py` + the `WRITE_VERBS`/read-only pins. SECURITY.md C10 + C15-a updated.
**Next rung:** the GUI app-store dialog that mounts create → configure → preview → stage.

### feat(actuation): never-brick install wrapper in the runner image (R5 PR-B, step 3 / I-2) (2026-06-25)
Extends the never-brick install posture (C15-a) to the **second install seam** — the Semaphore-runner image build
(`docker/semaphore-runner/Dockerfile`), the actual production install path. The raw
`ansible-galaxy collection install -r … -p /usr/share/ansible/collections` is replaced by the **same**
`scripts/install-collections.py` the bootstrap uses, so the signature posture can't differ between the two seams.
The wrapper gains two env overrides (`KONTROLL_GALAXY_BIN`, `KONTROLL_COLLECTIONS_PATH`) so it adapts to the
image's semaphore-venv `ansible-galaxy` + the `-p` install target without hardcoding the layout; bootstrap (I-1)
sets neither, so its argv is **unchanged** (byte-identical-to-today preserved). The Dockerfile now COPYs **both**
generated files — the lockfile + the trust sidecar (**BRICK-2**, so the wrapper can read per-collection policy in
the image). Keyring-gated like I-1: no keyring baked today → a plain install (the never-brick floor), zero
behaviour change on the all-unsigned fleet. Covered by `tests/unit/test_install_collections.py`: the env-override
path adds `-p` + the venv binary; the bootstrap seam (no env) stays the plain default; and the **G-4 no-drift
gate** (`test_dockerfile_installs_via_the_same_wrapper`) pins that the Dockerfile installs via the wrapper (not a
raw flat install) and COPYs the sidecar. Dogfood-gated (D2 image build all-unsigned succeeds / D4 tampered →
halts). SECURITY.md C15-a updated. **Next rung:** keyring auto-provisioning + the L2b wrapper-recorded re-verify
(both inert on today's unsigned fleet, to land with a signing-enabled source).

### feat(actuation): never-brick install wrapper at bootstrap (R5 PR-B, step 2 / I-1) (2026-06-25)
The first install step to actually apply the never-brick posture (C15-a). New `scripts/install-collections.py`
replaces the raw flat `ansible-galaxy collection install -r <lockfile>` at `ansible/playbooks/bootstrap.yml`: it
reads the trust sidecar and installs adaptively — verify a GPG signature **where the source serves one and a
keyring is held**, proceed where it's absent, fail **only** on a signature that fails to verify (tamper). The
wrapper is **keyring-gated**, which makes it safe to land now with zero risk on the current fleet: with **no
keyring on disk** (today's reality — public Galaxy serves no signatures, so no keyring is provisioned) it issues a
**plain `-r` install byte-identical to the prior behaviour**; a keyring present adds
`--required-valid-signature-count all --ignore-signature-status-codes NO_PUBKEY NODATA` (the source-traced
brick-proof mode), **never** a `+`-prefixed (fail-on-absence) count, and the ignore-list is fixed at
`{NO_PUBKEY, NODATA}` — never a tamper code. A failed install (a signature that doesn't verify) propagates
non-zero (fail-closed). The wrapper prints a loud, names/counts-only provenance summary (NB-4). Covered by
`tests/unit/test_install_collections.py` (no-keyring⇒plain / keyring⇒adaptive flags / never-`+` / ignore-list-is-
exactly-{NO_PUBKEY,NODATA} / failure-propagates / summary-is-counts-only) + `tests/unit/test_bootstrap_requirements.py`
(bootstrap installs via the wrapper, not the raw command). SECURITY.md C15-a updated. **Next (dogfood-gated):**
keyring auto-provisioning + the L2b wrapper-recorded `sha256` re-verify + the I-2 (Dockerfile) seam (its own PR).

### feat(actuation): never-brick supply-chain trust foundation (R5 PR-B, step 1) (2026-06-25)
The brick-proof data foundation for the supply-chain install hardening — built to the operator principle *a novice
must never have their deploy bricked because some publisher didn't sign a collection* (design of record:
`docs/reviews/2026-06-25-never-brick-supply-chain/`, a 7-agent blackboard with both adversarial reviews
GO-WITH-FIXES). Live-confirmed: public Galaxy serves **zero** signatures for all six fleet collections and a
`sha256` for every one — so a fail-on-absence (`+`-prefixed) flag would brick the whole fleet. The model:
**L1 `==` pin + L2 checksum unconditional/brick-proof; L3 signature adaptive** (verify-where-served,
pass-on-absence, fail-only-on-tamper). This step lands the **policy carrier + author-time guard** and changes
**zero install behavior** (so it's fully hermetic / CI-verifiable): `scripts/gen-requirements.py` now also emits a
generated, gitignored **trust sidecar** (`ansible/collections/trust.generated.yml`) carrying per-collection
`signature_policy` (default `adaptive`) + a `sha256` anchor — staying **offline** (the recorded digest is captured
by the future install *wrapper*, never fetched by this install-gating generator: synthesis Decision R1, closing the
brick-hunter's BRICK-1), with a new hermetic `gen-requirements --check` staleness gate (the only `gen-*` that
lacked one). `scripts/gen-actuation.py` adds the `adaptive` enum default-on-absence, keyring/`sha256` shape
validation, and the **Tier-cap** — a downloaded unit with unverified provenance (`signature != required`) may not
target `edge_firewall`/`core_switch` (fail-closed at generate, never on plain absence). Covered by
`tests/unit/test_actuation_registry.py` (the sidecar's adaptive default + the Tier-cap incl. on absent signature +
the **BRICK-1 no-network source pin**). New control **C15-a** + accepted-risk **R-NB-1** in SECURITY.md. The
install-wrapper enforcement (the L3 flag mode + the L2b wrapper-recorded re-verify + keyring provisioning + the
amber "unsigned source" surfacing) is the next, **dogfood-gated** rung; the Dockerfile (I-2) seam its own PR.

### feat(actuation): the FIRST app-store write verb — stage a configured unit (R5 PR-A) (2026-06-24)
The first actuation **write** in the GUI-actuation build: stage a configured unit as a proposal on the existing
two-key spine. `service/actuation.py` gains `apply_actuation_plan` (writes the unit's committed configure-values
file `instance/actuation/<key>/vars.yml` via the overlay target — **idempotent**, registered in `WRITE_VERBS`
same-commit so the #134 completeness gate + `assert_read_only` stay exhaustive) + `stage_plan` (recompute +
`verify_token` anti-drift + apply, mirrors `capability.promote`); `build_actuation_plan` now also returns the
staging metadata (`paths`/`vars_content`/`token_parts`/`plan_token`) yet **stays read-only** (it renders the
artifact string, the write is `apply_actuation_plan`). New **dedicated privileged** route `POST /actuation/{key}`
(`api/routes/actuation.py`, a `/actuation` prefix distinct from the write-free `/units` inquiry reads — so the
prefix-based rate-limit classifier + the auth boundary cleanly separate the write): `apply:false` proposes (plan
paths + an anti-drift token), `apply:true` stages the values file to `proposed/<run_id>` via
`commit_and_push(run_id)` — **never main**, never imports `promote_ref` (C10). Audited (`actuation-stage`,
names/run_id only); a leaked token can only park a rejectable proposal. SEC-2 holds (no secret value is ever
staged). Covered by `tests/unit/test_actuation_service.py` (apply/idempotent/drift/WRITE_VERBS) +
`tests/integration/test_api_actuation.py` (401/404/propose-token/scoped-commit+audit/stale-409/proposed-ref); the
rate-limit privileged-route classification tripwire was extended for the new prefix. The generated runnable
artifacts (the Semaphore template/wrapper) + the **signed** `ansible-galaxy install` (SEC-1) are the follow-on
rungs (R5 PR-B / R6); the GUI dialog that drives this lands with the dialog rung.

### feat(actuation): the unit PREVIEW / Review stage (R4) (2026-06-24)
R4 of the GUI-actuation MVP build: the Review stage's substrate — render what a configured unit WOULD run, still
write-free (the last read-only rung before R5's write verb). New `scripts/kontroll/service/actuation.py` with
**`build_actuation_plan`** (PURE, mirrors `observe.py`/`identity.py`'s `build_plan`): it loads the actuation unit,
re-validates the submitted configure values (reusing R3's `validate_unit_config`, fail-closed), and renders the
would-run play — the role→play **wrapper** (`hosts:` = the inventory_group, the FQCN role, the curated non-secret
vars, with a **derived access-chain header** naming the blast radius — design 22 §2.3/§2.4) for a `role` unit, or
the FQCN / standalone **run-spec** for a directly-runnable unit — plus the resolved vars, the `--check`-first
hand-off line, and the post-promote enact steps (Dry Run before apply). Behind a **write-free**
`POST /units/preview` (404 no_unit; 422 invalid values; stages nothing — design 24 §5). Read-only by construction
(the #134 completeness gate sees no write verb; `build_actuation_plan` + every render helper carry
`assert_read_only` pins). Secret-typed actuation vars stay forbidden at the descriptor layer (R3/SEC-2), so the
preview never carries a credential. Covered by `tests/unit/test_actuation_service.py` (render/validate/no_unit/
check-first/read-only) + `tests/integration/test_api.py` (the route). The GUI Review pane that mounts this lands
with the dialog rung; the `apply_actuation_plan` write verb + the generated change-set are R5.

### feat(actuation): the curated-knob CONFIGURE form (R3) (2026-06-24)
R3 of the GUI-actuation MVP build: the configure stage's substrate. An actuation unit's `unit.yml` gains an
optional **curated `knobs:` block** — operator-declared levers `{key, type, label, required, default, help}` +
the type options (`allowed`/`range`/`pattern`). This is the worked-extraction (report 40) mandate made concrete:
for this fleet (modules, ~zero argspec roles) the configure surface is **operator-curated knobs, NOT argspec
extraction**. New: `gen-actuation.py validate_knobs` (fail-closed — `type` in the server-validated set, a
**secret-typed actuation var is FORBIDDEN** (no per-unit SOPS domain; SEC-2), `text` needs a `pattern`, `enum` a
non-empty `allowed`, an `int` `range` must be min≤max, a curated `default` must itself validate); `configurable`'s
new closed **`actuation-unit`** kind → one `unit` knob group rendered through the EXISTING `renderKnobGroup`
(new `unit` source_kind → `renderKnob`, zero new widget code); `service/units.py::validate_unit_config` (the
server-side gate — a CLOSED allow-list + the P0a `_validate` guard per knob) behind a **write-free**
`POST /units/{key}/configure` (it validates, stages nothing — design 24 §4). Read-only by construction (the #134
completeness gate sees no new write verb; `_unit_view`/`_unit_knob`/`validate_unit_config` carry
`assert_read_only` pins). Covered by `tests/unit/test_actuation_registry.py` (knob validation),
`tests/unit/test_configurable.py` (the projection + pins), `tests/unit/test_units.py` (the validate gate),
`tests/integration/test_api.py` (the route), and `tests/e2e/test_knob_renderer.py` (the `unit` group renders).
The unit-dialog chrome (badge → Pick → Install stages) + the GUI configure pane that mounts this form land with
the dialog rung; the push/stage write verb lands at R5.

### feat(actuation): the app-store unit registry + requirements-union (R2) (2026-06-24)
R2 of the GUI-actuation MVP build: the download/pin stage's data foundation — the **third drop-in registry**
(`actuation/<key>/unit.yml`, beside `modules/` and `capabilities/`). A unit is a chosen, version-pinned runnable
thing (collection playbook / role / in-repo standalone); its `install.collections` **derives** into the lockfile
so the no-hand-edit rule still holds. New: `gen-requirements.py` unions every ACTIVE unit's pins (the
`resolve_pins` policy — an app-store EXACT `==` pin WINS over a module floor; two differing exact pins for one
collection FAIL CLOSED; string-equality only, no SemVer floor math); `gen-actuation.py` is the fail-closed
descriptor validator (known schema, closed `kind` enum, `key`==dir, exact pin, target class is an enabled module,
closed blast/provenance enums; `git` source rejected) wired into `tests/validate` (`--check`); `catalog`'s
`load_actuation_units`/`registered_actuation_units` read the **instance overlay** (`instance/actuation/`, stripped
from the public tree). **Doctrine widening (CLAUDE.md hard rule):** collections now derive from "enabled modules
**and active actuation units**." Write-free, blast-free — with zero units present the lockfile is byte-identical
(verified); the push-stage template/wrapper generation + the hardened signed install land at R4/R5. Covered by
`tests/unit/test_actuation_registry.py` (resolver / union / validator / overlay loader).

### feat(api): GET /units/{collection} — enumerate a collection's runnable units (R1) (2026-06-24)
R1 of the GUI-actuation MVP build: the app-store "search → unit" seam. A search result is a COLLECTION; this
read-only route projects it into the LAUNCHABLE units the later configure/push stages act on — collection-shipped
playbooks + roles (NOT modules, which are tasks, not runnable on their own). New: `probe.units_in_collection`
(files-only enumeration, no ansible-doc), `service/units.py::service_units` (resolver), `GET /units/{collection}`
(typed `UnitsResponse`; 404 if not installed; empty `units` when the collection ships only modules — the
device-management fleet's case, per the worked extraction). **SEC-3 traversal guard:** `service_units` resolves the
`{collection}` param against the enumerated installed set (`catalog._installed_json`) and only then walks that
collection's own dir — a `../`/absent/crafted name is a 404, never a filesystem read outside the installed
collections. Read-only by construction (the AST completeness gate sees no new write verb; `service_units` carries an
`assert_read_only` pin). Covered by `tests/unit/test_units.py` (prober + resolver + guard-never-walks) and
`tests/integration/test_api.py` (route 200/404 + the typed `/units/{collection}` path in `/openapi.json`).

### refactor(ansible): collapse bootstrap's collection resolution onto the single-source generator (2026-06-24)
`bootstrap.yml` carried its own inline Jinja requirements-merge (seed from `modules/_core.yml`, loop-merge each enabled
module's `collections`, render `requirements.generated.yml` from an `all_collections` fact) that duplicated
`scripts/gen-requirements.py` — the resolver `deploy-stack.yml` and CI already run. That two-derivation-path divergence
hazard meant a change to the resolution/dedup logic in one place could silently diverge from the other, so the runner
image could bake a different collection set than CI validated. bootstrap now calls `gen-requirements.py` (the lockfile
is byte-identical), retaining only the `secrets_domains` collection the generator does not own. R0 of the GUI-actuation
MVP build (docs/reviews/2026-06-24-gui-actuation-design/, F4) — read-only, no new write verb. Pinned by
`tests/unit/test_bootstrap_requirements.py` (the generator call precedes install; the inline `all_collections`
resolution stays gone).

### fix(gui): keep the capture store's .gitignore out of the backup viewer device index (2026-06-24)
The C14 backup viewer's `list_devices()` indexed every TRACKED file in the capture-history store, including the store's
own `.gitignore` (backup-configs.yml commits it to carry the capture-exception excludes), so the GUI device index
showed a bogus `.gitignore` row (label `.gitignore`, host_key empty) beside the real device captures — found live during
the 2026-06-24 C14 prod dogfood. Root cause: `_ondisk_files` already excluded `.git*` but `_tracked_files` (`git
ls-files`) did not. Added a shared `_is_capture_file` predicate (no filename starting with `.`) used by BOTH, so a
committed dot/meta file is uniformly kept out of the index AND the `_validate_file` allow-list — the allow-list
TIGHTENS (`.gitignore` is no longer viewable/diffable), which only narrows the read surface (no trust-boundary move, so
no SECURITY.md change). Read-only (no new write verb; the AST read-only pins still pass). The `tmp_captures` test
fixture now commits a `.gitignore` to mirror the real store; covered by `test_list_devices_excludes_the_gitignore_meta_file`.

### fix(ansible): make capture-history git mutators uid-agnostic via scoped safe.directory (2026-06-24)
The capture-history store (`config_backup_dir`) is owned by the Semaphore runtime uid (1001 — the scheduled writer).
When an operator hand-runs `backup-configs.yml` on the host to debug or seed a first capture, they run as a DIFFERENT
uid (root via sudo, or the uid-1000 operator — uid-1001 has no host passwd entry), so every raw-git command aborts
`fatal: detected dubious ownership` and the snapshot commit never lands — surfaced live during the 2026-06-24 C14 prod
dogfood (the commit had to be finished by hand). The three mutating raw-git tasks (`rm --cached`, `add -A`, `commit`)
now carry `-c safe.directory={{ config_backup_dir }}` — **scoped to that one store, never `*`** — making them
uid-agnostic. It is a no-op on the same-uid runner path (the codified backup chain is unchanged) and grants no write
capability (it only suppresses the cross-uid guard for one dedicated dir). `git init` is intentionally exempt (it
creates the repo). Static tests pin the flag onto the mutators + the no-wildcard scoping. Audit:
docs/reviews/2026-06-24-deploy-codification-audit/.

### feat(repo): scripts/update-from-bundle.sh — codify the remoteless bundle code-update (2026-06-24)
`scripts/update.sh` is the state-preserving merge/re-seed update path but assumes a live `origin` remote; a production
box in the local-as-truth posture (no GitHub remote) can't `fetch origin`, so the operator had to hand-run the #145
git-bundle dance (bundle → scp → fetch → merge → push canonical). Added `scripts/update-from-bundle.sh`, a thin sibling
of `update.sh` that swaps `fetch origin` for `git fetch <bundle>` and reuses the IDENTICAL `git merge --no-edit` +
`push local HEAD:refs/heads/main` body (same conflict surfacing, same canonical re-seed). Codifies the previously
hand-run remoteless sync; docs in `docs/local-source-of-truth.md` (Phase B) + `scripts/README.md`.

### test(security): fail-CLOSED read-only completeness gate — every write verb is registered (P0b, #134) (2026-06-24)
The read-only AST pins (`assert_read_only` #133, `assert_no_git_write` #131) fail OPEN: each checks a function's
calls against `WRITE_VERBS`, so a mutator the registry doesn't KNOW about is invisible — a read view could call an
unregistered writer and the pin would pass, silently eroding the read-only / C10 two-key guarantee. Added
`assert_write_verbs_complete` (+ `assert_write_verbs_resolve` no-rot): it AST-scans the pinned layer
(`scripts/kontroll/service/*.py` + `gitio.py`) for every DIRECT mutator — a write-mode `open`, a git-MUTATING argv
(incl. the `["git","add"] + paths` form), a SOPS write — and fails CI if any isn't in `WRITE_VERBS`, making "register
the verb in the same commit" an ENFORCED gate, not a convention. The sweep surfaced + closed **seven real gaps** the
old manual list had missed — most importantly **`sops_write_domain`, an UNPINNED secret-writer** (a read view calling
it would not have been caught), plus the four `apply_plan` reconfigure dispatchers, `apply_telemetry_plan`/
`apply_logging_plan`, `add_capture_exception`, `_write_new`, and `service_refresh`. The gate uses a stricter
always-mutating git-verb set than the `assert_no_git_write` blacklist so a dual-use read (`git remote get-url`,
`git config --get`) doesn't false-positive a read helper. Proven against planted violations (a write-open the
registry doesn't know is flagged; a pure read is not).

### feat(gui): read-only redacted backup index/view/diff viewer — the C14 gated surface (C1, #131) (2026-06-24)
The operator could declare + run config backups (#123/#125) but had no way to SEE them — to answer "what did this
device's config look like last week, and what changed?" Added a **read-only** per-device backup index → redacted
single-rev view → unified diff over the secret-bearing capture git store, opened from a new header **Backups**
button (flat list) or a per-host `fleet-backup-open` drill-in on the Index. New `service/backups.py` (a pure
`git log/show/diff` read — DISTINCT from `service/backup.py`, the capability *builder*), `config/capture-redactions.yml`
(a sparse per-vendor secret-mask registry, sibling of capture-exceptions.yml), four `@require_auth` audited GUI-only
routes, and the modal (`renderDiff` paints the server's structured hunks; `textContent` everywhere — zero XSS). This
is the riskiest GUI surface (it serves crown-jewel secrets over HTTP), so it is **fail-CLOSED by construction** and
ships behind a binding security GATE (SECURITY.md **C14**): `:ro` captures mount (compose-pinned on the literal
`:ro` suffix), read-only-by-construction (AST + argv git-verb pins), rev/file validated before any `git show`
(traversal/injection closed), **redacted-by-default + redact-then-diff** (defense-in-depth, NOT the boundary — the
boundary is auth+TLS+mgmt-bind+`:ro`), every read audited NAMES-only. The redaction registry is validated against
the **real lab vendor token shapes** (Cisco/FortiGate/OPNsense/RouterOS); the secret-densest, history-excluded
vendor (FortiGate) is listed but **never rendered** (its on-disk blob stays off the wire). Raw download + split
diff are deferred (C2). G5 (human security sign-off) is the last gate before ship.

### feat(ansible): enforce the backup retention window — the #125 history-prune play (2026-06-23)
#123 declared + carried a `retention` window (keep-all/30d/90d/365d) to the scheduled backup run as
`kontroll_backup_retention`, but NOTHING enforced it — a finite window was a recorded intent only. Added a guarded
prune play to `backup-configs.yml` (after the snapshot-commit play): it re-roots the capture-history git onto a
synthetic full-snapshot baseline, dropping commits older than the window, then reflog-expires + gc's to reclaim
disk. Each capture-history commit is a full-tree snapshot, so pruning loses only the diffable HISTORY beyond the
window, never the latest capture. **Default-safe + fail-closed:** `keep-all`/undefined ⇒ a 0-changed no-op; a bad
hand-run value (e.g. `7d`) fails loudly; the prune refuses unless the target is the **local-only captures repo**
(real worktree, NO git remote, not bare, not the canonical `/srv/kontroll.git`/deploy tree — the no-remote check
is the load-bearing distinguisher), and it stays **check-skipped** under `--check` (the read-only guards run; the
writer does not). Idempotent: a re-run in the same window drops nothing (a drop-set-count gate). Designed via an
ultracode blackboard (`docs/reviews/2026-06-23-backup-history-prune/`); the synthesis caught + fixed an
idempotency bug in the first mechanism draft. Covering tests: `tests/unit/test_backup_prune.py` — 6 structural
(gating, window-map==`_RETENTIONS` drift guard, fail-closed no-remote guard, `--check` boundary, ordering, bad-value
reject) + 3 behavioral (run the embedded prune shell on a real tmp repo: only pre-cutoff commits drop, the latest
tree is byte-identical, idempotent, never-empty). Docs: SECURITY.md C8; `service/backup.py` + `capabilities/backup.yml`
flip #125 deferred → enforced. The prune touches commit metadata/trees only — never prints capture content.

### fix(ansible): provision the gid-1001 group + operator membership + owner in local-canonical.yml — close F-CANON (#119) (2026-06-23)
The C10 canonical `/srv/kontroll.git` is group-1001 (the runtime uid:gid the privileged API/runner writes as),
but a FRESH control node has no named group at gid 1001 and the operator is not a member — so `local-canonical.yml`'s
Mirror push (run as the operator, no `become`) died `Permission denied: unable to write objects` the moment any
canonical object was group-1001-owned but not operator-owned. The #119 prod cutover needed a manual
`groupadd -g 1001 kontroll; usermod -aG 1001 admin; chown -R admin:1001 /srv/kontroll.git`. Landed those as
idempotent tasks: (1) ensure the `kontroll` group (gid 1001), (2) add the operator (append, supplementary), and
(3) the recursive write-grant now sets **owner=operator** as well as group=1001. The owner half is load-bearing —
a freshly added supplementary group is not effective in the operator's current login session, so the first-run
push succeeds via OWNER perms regardless. The previous prod box never hit this (it ran no onboard-gui that staged gid-1001
objects). Covering test: `tests/unit/test_local_canonical.py` (group gid-1001, operator append-membership,
recursive owner=operator, and the ordering-before-push). Docs: `docs/install-from-scratch.md` §3.

### fix(secrets): correct the Grafana rotation enact for Grafana 13 — `grafana-cli` → `grafana cli` (#141 PR-3) (2026-06-23)
The `grafana_admin_password` actuation emitted `docker exec -i grafana grafana-cli admin reset-admin-password`,
but Grafana 11+ removed the standalone `grafana-cli` binary (it is now the `grafana cli` subcommand) — verified
live on the deployed `grafana/grafana:13.0.2` (`grafana-cli` absent; `grafana cli admin reset-admin-password
--password-from-stdin` present). The emitted command would have failed if an operator ran it. Fixed the
descriptor `exec:` to `grafana cli admin reset-admin-password --password-from-stdin` (the flag keeps the new
value OFF argv — the operator pipes it on stdin). It stays an **operator host-shell** step: the Semaphore runner
holds no docker socket, so it cannot exec into Grafana. A true one-click Semaphore task would need a standing
Grafana admin service-account token (the runner can reach Grafana's HTTP admin API but cannot authenticate after
a rotation otherwise) — deliberately **parked**, not built. Test `test_actuation_enact_recreate_and_grafana_cli`
now pins the corrected form (no `grafana-cli`; has `grafana cli … --password-from-stdin`).

### feat(secrets): live-rotation actuation for the Semaphore admin login — the `semaphore-admin` enact kind (#141 PR-2) (2026-06-23)
PR-1 left `semaphore_admin_password` rotatable but **stage-only**, with a forward note to add a "recreate"
actuation. Live inspection of the running image (v2.18.12) corrected that plan: Semaphore applies
`SEMAPHORE_ADMIN_PASSWORD` only at **first-run `semaphore setup`** (config.json absent), so a plain recreate
does **not** rotate an existing admin — the same trap as Grafana. The deterministic, purpose-built path is the
in-container `semaphore users change-by-login`.
- **New closed-enum actuation kind `semaphore-admin`.** `actuation_enact_commands` now emits a two-step
  post-promote hand-off for the field: (1) `deploy-stack -e stack_services=[semaphore]` re-renders `.env` (the
  promoted value lands in the container env) and recreates; (2) `docker exec semaphore sh -c 'semaphore users
  change-by-login …'` upserts the live login, reading `$SEMAPHORE_ADMIN`/`$SEMAPHORE_ADMIN_PASSWORD` from the
  container's OWN env — the value never reaches the host argv or shell history. The GUI/API still execute
  nothing (no docker socket); the C9/C10 boundary is unchanged. Verified live (change-by-login resolves the
  admin, env-ferry + config path confirmed, non-destructive).
- **Drop-in.** `secret-forms/semaphore.yml` gains the `actuation: {kind: semaphore-admin, service: semaphore}`
  block — no hub edit; the existing per-FIELD allow-list still keeps the DB-password/access-key rotation traps
  off the surface.
- **Tests:** `tests/unit/test_secrets_service.py` — the `semaphore-admin` two-step enact (recreate + env-ferried
  change-by-login, no embedded value, recreate consequence); restructured the stage-only coverage so a
  non-rotatable field and a rotatable-**without**-actuation field both still emit nothing.

### feat(gui): rotate a service login from the Secrets dialog — overwrite-confirm + actuation hand-off (#141) (2026-06-21)
The operator had no first-class way to change a running service's password (Grafana/Semaphore/onboard-gui/API) —
it meant a shell `sops` ritual. The Secrets dialog now ROTATES a `rotatable:` login field, reusing the existing
mint+stage path (`build_secret_plan`/`apply_secret_plan` → SOPS ciphertext → C10 `proposed/<run_id>`). Two
net-new PURE service functions: `secrets.overwrite_set` (NAMES of already-set fields a save would clobber,
re-derived server-side) and `secrets.actuation_enact_commands` (the post-promote command strings, mirroring
`observe.telemetry_enact_commands`).
- **Anti-clobber (C9 M-R-B).** Re-saving an already-set field fails CLOSED (HTTP 409 / `secret-overwrite-confirm`)
  until the caller acks `overwrite:true`; the confirm + the `secret-apply` audit's new `overwrite=` marker carry
  field NAMES only — never a value (C11/C12). The server is the guard (M1); the client gate is convenience.
- **Actuation is a HAND-OFF, not a GUI action.** A staged value is not live until the operator/Semaphore runs the
  emitted enact strings (`deploy-stack` recreate for onboard-gui/API; `grafana-cli` reset for Grafana). The
  GUI/API execute nothing and hold no docker socket — the C9/C10 trust boundary is unchanged.
- **Drop-in, per-FIELD allow-list.** Adding a rotatable service = `rotatable:` + an optional `actuation:` block on
  a `secret-forms/<domain>.yml` field — no hub edit. The semaphore DB password + access-key are left un-flagged
  (rotation traps). MVP-1: onboard-gui + API token actuate (clean recreate); Grafana ships stage + the
  `grafana-cli` enact string; Semaphore-admin is stage-only (its live actuation lands in PR-2 — see the
  `semaphore-admin` entry above; the live mechanism turned out to be a CLI upsert, not a plain recreate).
- **Generated fields re-mint only on an EXPLICIT signal (footgun fix, was a deferred follow-up).**
  `build_secret_plan` mints a `generate:` field only when it is in the new `regenerate` list (the GUI `gen`
  button) OR ABSENT (first-time); a blank already-set generated field is KEPT — so rotating one field no longer
  silently re-mints (and self-invalidates) the privileged `kontroll_api_token`. The `gen` button now MARKS the
  field for regeneration instead of relying on blank=mint; `kontroll-init` is unaffected (it passes no
  `regenerate`, and absent fields still mint). Adversarially reviewed (`docs/reviews/2026-06-21-secret-rotation-review/`).
- **Tests:** `tests/unit/test_secrets_service.py`, `tests/integration/test_api_secrets.py` + `test_gui_api.py`
  (409 gate, `overwrite=` audit value-free, enact API/GUI parity, `regenerate` re-mints only the listed field),
  `tests/e2e/test_secret_rotation_flow.py`.

### fix(ansible): `deploy-stack.yml --check --diff` reaches the .env render — read-only resolvers run in check mode (2026-06-21)
Caught LIVE 2026-06-21 arming the onboard-gui GUI propose path on prod: `ansible-playbook … --check --diff` (the
CLAUDE.md-mandated dry-run before any state-changing deploy) died at the "Render docker/.env" task with `could not
locate file in lookup: /semaphore.sops.yml`. Root cause: `_secrets_dir` is the play var
`{{ _secrets_dir_resolved.stdout }}`, set by a read-only `ansible.builtin.command` (`paths.py resolve`) — and a
plain `command` is **SKIPPED under `--check`**, so the register stayed empty, `_secrets_dir` collapsed to `''`, and
every downstream `community.sops.sops` lookup got an absolute `/semaphore.sops.yml`. The stack's most important
playbook was thus un-dry-runnable, undercutting the "always `--check --diff` first" hard rule.
- **Fix:** tag each read-only RESOLVER (`_secrets_dir_resolved`, `_uvicorn_extra`, `_needs_cert`, `_runs_caddy`,
  `_journal_grp`, `_caddyfile`) `check_mode: false` so it executes under `--check` too — they only resolve a
  path/flag/GID (no mutation), so it is safe and a no-op on real runs. Mutating tasks (the `gen-*` regenerators,
  clones/chowns, compose `up`) deliberately STAY check-skipped so a dry-run never actuates.
- **Test:** `tests/unit/test_deploy_stack_check_mode.py` pins `check_mode: false` on every resolver (by `register:`
  name, recursing into blocks), pins the keystone `_secrets_dir_resolved`, and pins the resolver-vs-mutator boundary
  (the `up`/`gen-*` writers must never be `check_mode: false`).

### fix(ansible): device-CA-bundle / file_tail known_hosts deploy task no longer fails on the no-pin default (2026-06-21)
Caught LIVE during the #119 prod cutover. `deploy-stack.yml` assembles the operator-pinned device CA bundle (and
the file_tail_ssh known_hosts) via `ansible.builtin.copy` with `content:` rendered from a `{% for %}` over
`instance.yml device_trust.<key>.tls_ca_file`. With **no** CA/host key pinned — the DEFAULT `self_signed` /
no-`device_trust` posture (e.g. any genericization-era `instance.yml`, which has no `device_trust:` block) — the
loop rendered to `""`, and `ansible.builtin.copy` on **Ansible 2.19+** rejects empty content with `src (or
content) is required`, **failing the whole deploy** the moment `prometheus`/`vector` is in `stack_services`. CI
never caught it (no live deploy) and prior dogfoods pinned a CA or deployed a subset that skipped the task.
- **Fix:** SPLIT each bundle into a pinned-case loop task (`when … length > 0`) and a no-pin task that writes a
  literal single-newline file (`when … length == 0`), so the default always writes a valid **whitespace-only**
  file `copy` accepts (PEM/known_hosts parsers ignore it). A naive trailing-literal-newline fix is dead — Ansible's
  Jinja `trim_blocks=True` strips the newline right after `{% endfor %}`. Both isomorphic tasks fixed.
- **Test:** `tests/unit/test_deploy_stack_ca_bundle.py` pins that each bundle has a no-pin (`length == 0`) task with
  non-empty content and that the loop task is gated `length > 0` — so the no-pin default can't re-hit empty `content`.

### feat(dashboards): generate the Homepage **Fleet** tile group from the onboarded fleet (B3, #130) (2026-06-21)
The `:3000` portal's device tiles were hand-maintained — so an onboarded device only appeared on the board if the
operator also hand-edited `services.yaml`, the kind of drift the "generated, never hand-maintained" doctrine
exists to kill. `scripts/gen-homepage.py` now generates a **Fleet** group from the same modules × inventory join
the scrape targets use (`module.inventory_group` × the hosts in that group), one `href:`+`description:` tile per
onboarded host — so adding a device to the fleet adds its portal tile on the next `deploy-stack`, zero hand-edit.
- **One owned span, shared with the editor.** The tiles live between `# >>> kontroll fleet … >>>` / `# <<< kontroll
  fleet … <<<` markers *inside* the operator-owned `services.yaml`; everything outside is preserved byte-for-byte.
  The marker boundary is defined once in `service/homepage` (the new pure `splice_fleet_block` + the existing
  `_split_services`), the SAME module the `:8443` Homepage editor (#136) consumes — so producer and consumer can't
  drift: the editor flags the generated group read-only and refuses to edit its tiles (#124 ownership). The tile
  derives `https://<mgmt-addr>`; a class may override scheme/port/icon with an optional `homepage:` block in its
  `module.yml` (fanned like `metrics:` — no module churn required).
- **Example-driven, secret-free, no-leak (F3 doctrine).** `--check`/`--example` regenerate the committed PUBLIC
  span in `instance.example/dashboards/homepage/services.yaml` from `instance.example/` (TEST-NET `192.0.2.x`
  only — it ships; only `instance/` is stripped); the bare write (deploy-stack) splices the private `instance/`
  overlay live, never committed to origin. Tiles are `href:`+`description:` only — never a `widget:`/secret (C11);
  a tile value carrying a newline or secret marker fails the generator closed.
- **Tests/Docs:** `tests/unit/test_gen_homepage.py` (the staleness + TEST-NET/secret gate over the shipped example
  span, the producer↔editor agreement — the emitted block parses as one opaque `read_board` generated section the
  editor refuses to edit — empty-fleet, idempotency, injection-refusal, the optional descriptor) + a
  `splice_fleet_block` pin in `test_homepage`; registered in `tests/validate.sh` (`gen-homepage --check`) and
  `deploy-stack.yml` (regenerates before Homepage `up`, `when 'homepage' in stack_services`); `modules/README.md`
  + the homepage `README.md` synced (the portal tile is gen-homepage's, not gen-observability's). SECURITY.md C11
  notes the generated span is secret-free by construction.

### fix(observability): committed generated artifacts derive from the PUBLIC example inventory, not the real overlay (2026-06-20)
**F3**, caught by the live GUI dogfood. The committed `prometheus/targets/*.generated.yml` +
`docker/vector/generated/*.generated.yaml` are repo-level (NOT under `instance/`, which `make-bundle` strips), so
they **ship in the public release bundle** — but they were generated from the maintainer's PRIVATE `instance/`
overlay and committed, **leaking the real homelab topology** (real RFC-1918 addresses and device hostnames) into every bundle. Root cause: the generator + the staleness `--check` + the pinning tests all
keyed off the tracked-real `instance/` overlay (the `instance.example/` fallback is Phase-5/#72 work), so the
whole chain was real-valued and mutually consistent.
- **Fix:** regenerated the committed artifacts from `instance.example/` (TEST-NET `192.0.2.x` only), and re-keyed
  the staleness gate to the PUBLIC source. `scripts/gen-observability.py` / `gen-logging.py` gained an `--example`
  mode; both `--check` (the validate gate) and `--example` now read `instance.example/`, while the **bare** write
  (deploy-stack, live) keeps the instance OVERLAY — so a running node still regenerates its real targets locally,
  never committed to origin. `test_gen_observability` / `test_gen_logging` assert *committed ==
  generator(instance.example/)*. The maintainer's deploy still regenerates live artifacts from their private overlay; those
  are instance state, never committed to origin.
- **Leak-regression gate:** new `tests/unit/test_no_topology_leak.py` fails CI if any committed generated artifact
  ever carries an RFC-1918 private IP again — so a real-overlay regeneration committed by mistake can't recur.
- **Also genericized** the real IPs in `tests/e2e/inventory.yml`, `test_validate.py`, `test_suggest_logging_probe.py`,
  the `field-host` GUI placeholder, and `docs/observability/telemetry-method-registry.md` (doc-sync for the test
  pins). Prose lab-description in `PLAN.md`/`README.md`/`SECURITY.md`/role comments/`docs/` is **deferred to #72**
  (the genericization plan scopes prose de-leak + the `instance/`-untrack to the Phase-5 fresh public cut).
- **Verified:** `git grep` of `prometheus/`+`docker/vector/generated/` is clean; a rebuilt bundle carries only
  TEST-NET addrs; full `not e2e and not slow` gate green.

### fix(gui): the staging content clone rolls back to canonical main after each stage (2026-06-20)
**F1**, caught by a live dogfood of the GUI-paradigm arc on a real deploy. `gitio.commit_and_push` committed each
staged reconfigure on the onboard-gui/API content clone's local `main`, advancing it, and **never reset it back**
to the canonical `main`. So the clone's working tree — the GUI's read-back ("current") source — drifted forward
with every stage: proposals **stacked** (proposal B's `proposed/<run_id>` = `main + A + B`), a **rejected** edit
(its canonical ref deleted) kept contaminating later diffs/no-drop, and — because promote is fast-forward-only —
promoting B silently also enacted the abandoned A. The **C10 boundary held throughout** (the GUI never writes
`main`; staging + two-key promote intact); this was a correctness/safety gap in proposal **content**, not a
privilege escape.
- **Fix:** `commit_and_push` now returns the content clone to `local/main` (`git fetch local && git reset --hard
  local/main`) after a staged push — and after a failed staged commit, to drop the half-written tree. Each
  proposal is now **independent** off the canonical main (`promote_ref` already expects "re-propose against
  current main" once main moves). **Staging-only:** the operator CLI (target `main`) is never reset — it advances
  `main` directly and owns its working tree.
- **Tests:** `tests/unit/test_staging_isolation.py` drives TWO stages through ONE persistent real clone (the
  fresh-tmp-repo-per-test unit suite can't see cross-request drift) + a seam test pinning the staging-gate;
  `_reset_content_clone_to_canonical` → `WRITE_VERBS` (P0b).

### feat(gui): platform Settings EDIT + the three danger guards (2026-06-19)
Phase 5 of the GUI-paradigm plan (#140), **the highest-blast surface, shipped last**. Makes the Phase-1
read-only Settings panel EDITABLE — every write rides the **same** reconfigure spine + severity gate as goals 2
and 4b, and STAGES `proposed/<run_id>` (C10 — the GUI never promotes).
- **Editable knobs (data, not code):** new `settings/instance.yml` + `settings/fleet.yml` descriptors declare the
  knobs the Settings page may stage, each with its validator type, reconfigure severity/blast-radius, the writer,
  and a consequence-naming `confirm_text`. `service/settings.build_plan`/`apply_plan` + `build_fleet_disable_plan`
  ride `_reconfig.run_promote` (token → `verify_no_drop` → apply); the no-drop runs over the **full** instance.yml
  (a writer that dropped a co-owner key fails closed — the 4b review lesson carried).
- **The three GUARDs:** `mgmt_ip` (severity `identity` → **type-to-confirm** the new IP + the P0a `ipv4`
  validator + the re-IP "your session disconnects, reconnect at the new IP" consequence), `tls_mode`/`domain`
  (severity `redeploy` → the ack gate + the per-mode / re-render consequence), and `enabled_modules` **removal**
  (severity `remove` → the drop-monitoring consequence). `backup_remotes` is the low-blast EDIT proof
  (comma-separated list, per-item-validated). The consequence text renders in a `reconfig-consequence` pane.
- **New comment-preserving writers:** `_blockwrite.set_nested_scalar` (frontend.tls_mode), `set_list_block`
  (backup_remotes), `disable_in_fleet` (fleet removal — **RAISES, never sys.exit**, and the same fix lands on
  `onboard.enable_in_fleet`'s `sys.exit` relic, S-3/G9). `disable_in_fleet` → `WRITE_VERBS`; the new reads
  (`current_value`, the two configurable settings projections) are pinned read-only.
- **FORBID (M7, unchanged):** `api_privileged` (the C10 arming switch), `docker/.env`, and `.sops.yaml`
  recipiency have **no** edit control — a settings knob NOT in the descriptor set is a 404 (`unknown_knob`); the
  status group surfaces arming read-only. No `.env`/`-e`/profile IaC editor, no arming toggle, no second
  credential surface, no auto-deploy button. No secret value is read, returned, or written (C11).
- **GUI:** the Settings panel's identity/backup/fleet rows gain `edit`/`disable` controls
  (`settings-knob-edit`/`settings-fleet-disable`) opening the shared reconfigure dialog; the consequence warning
  renders above the diff. `/api/settings/<knob>` + `/api/settings/fleet/<module>` mirror the 4b routes.
- Covered by `test_settings.py` (mgmt_ip→identity + comment-preserving, tls_mode nested, backup_remotes list,
  crafted-value/unknown-knob reject, full-struct no-drop, fleet removal), `test_blockwrite.py` (the three
  writers), `test_fleet_edit.py` (enable_in_fleet now raises), `test_gui_api.py` (both routes stage
  `proposed/<run_id>` + FORBID knob 404 + stale-token 409 + the projections), `test_settings_edit_flow.py` e2e.
  testid_reference / SECURITY.md (new control) synced.

### feat(gui): heavier reconfigure — module identity + inventory host-var (2026-06-19)
Phase 4b of the GUI-paradigm plan (#139). Extends the Phase-4a reconfigure primitive to the two higher-blast
surfaces, on the **same** propose→token→promote→stage spine — single-object only (synthesis M6 cuts the aggregate
orchestrator). It also lands the descriptor-metadata half design 22 §10 wanted (Phase 3 / M5 deferred it).
- **Per-knob severity metadata (the §10 contract):** new repo-level descriptor dir `settings/<area>.yml` (a
  sibling of `capabilities/`, **not** instance config) carrying each reconfigurable knob's `severity`/
  `blast_radius`. `catalog.load_knob_descriptors`/`knob_meta` feed it in. `_diff.compute_changes(current,
  proposed, knob_meta)` now (a) also diffs a **flat `{key: scalar}`** shape (identity/settings), and (b)
  **UPGRADES** a `modify`/`remove` of a declared knob to `severity: identity` (rank
  add<modify<remove<redeploy<identity). An `add` (first-set) is never upgraded. Phase-4a callers pass no
  `knob_meta` → byte-identical.
- **Module-identity reconfigure (G7):** new `service/identity.py` + `_blockwrite.upsert_top_level_key`
  (comment-preserving top-level-key line-surgery) re-classify a class's `secrets_domain`/`inventory_group`/
  `backend`/`role`/`status` in `modules/<key>/module.yml` — was a whole-file Overwrite cliff. At most ONE
  identity-severity change per proposal (so the type-to-confirm is unambiguous); a `status` modify may ride
  along. `/api/identity/<key>` mirrors `api_capability`; severity `identity` fires the **type-to-confirm**
  `reconfig-identity-confirm`.
- **Inventory host-var reconfigure (G3):** `gitio.owned_merge` extracted from `write_inventory_host` — the silent
  key-level `.update()` now REPORTS the overwritten vars (collisions); onboard's add-a-host behaviour is
  byte-unchanged. New `service/hostvars.py` edits a host's `ansible_host` (MVP — the "device moved" case;
  validated by the P0a `ipv4` guard), carrying the host's other vars (incl. the **templated cred-lookups, which
  stay read-only** — M-R-B) through untouched. `/api/host/<key>/<host>`; severity `modify`.
- **Shared promote + the one dialog:** `_reconfig.run_promote` (token → `verify_no_drop` → apply) is the single
  promote half identity/host use (the capability spine is its pre-existing twin). The GUI gate `capReconfigGate`
  is generalized to `reconfigGate(dlg, changes, promoteBtn)` — it fires the right-weight confirm for the strongest
  severity (`reconfig-redeploy-confirm` ack / `reconfig-identity-confirm` type-to-confirm now render). One
  data-driven `openReconfigureDialog` (the shared knob renderer + diff pane + gate); entry points are the Index
  fleet card's `⚙ identity` badge + per-host `edit IP` control.
- **M1 (unchanged doctrine):** the token is anti-DRIFT only; the anti-clobber controls remain `verify_no_drop` +
  the human severity confirm + C10's two-key promote. Severity is re-derived **server-side** for the audit. New
  write verbs → `WRITE_VERBS` (`owned_merge`, `run_promote`; the pure line-surgery transforms stay out); the new
  reads (`identity`/`hostvars.current_values`, `configurable._identity_view`/`_host_view`) are pinned read-only.
- Covered by `test_diff.py` (flat-shape + the severity-upgrade, add-never-upgraded, no-downgrade),
  `test_blockwrite.py` (`upsert_top_level_key`), `test_identity.py` + `test_hostvars.py` (the instances +
  `owned_merge`), `test_gui_api.py` (both routes + the projections), and `test_reconfigure_flow.py` (the
  identity type-to-confirm + redeploy-ack e2e). SECURITY.md C9 residual updated.

### feat(gui): safe reconfigure — change a capability knob without a silent clobber (2026-06-19)
Phase 4a of the GUI-paradigm plan (#138), **the crux**. Today every GUI write is a *first-write*: a declared
telemetry/backup/logging block was a wall (`already_declared`). Goal-2 makes "change a knob of an
already-configured object" routine — and the danger it turns from rare into routine is a **silent value-clobber**.
This adds the safe-reconfigure primitive (capability blocks only): read-back the current values → compute a
field-level **diff** → surface it → gate the write behind a severity confirm + a machine no-drop invariant.
- **Read-back:** new pure `current_values(key)` per instance (observe/backup/logsvc); `capability.suggest_view`
  now also returns `reconfigurable_methods` (the declared methods, with schemas) + `current`, and the
  `/api/configurable` projection **pre-fills** each declared knob with its current value. The picker marks a
  declared method "· declared (reconfigure)".
- **Diff:** new `service/_diff.compute_changes(current, proposed)` → a field-level change-set
  (`changes[{path,kind,before,after}]` + `will_overwrite` + `severity` = max of add<modify<remove). The
  capability `build_plan`s now branch **add / no-op / upsert** instead of refusing; the propose response carries
  the diff and the dialog renders it in a new `reconfig-diff` pane.
- **Upsert writer:** new `_blockwrite.upsert_into_block` — a comment-/sibling-preserving line-span REPLACE (list
  block keyed by `method:`, mapping block for backup), never a `safe_dump` flatten. The closed-allow-list
  `validate_params` (P0a) still gates every upserted value, so the config-injection guard survives the
  reconfigure path.
- **The three anti-clobber controls (M1 — the token is anti-DRIFT, NOT authorization):** (1) `verify_no_drop`
  (new `service/_reconfig.py`, generalized from `keygen._verify_additive`) — the spine refuses at promote
  (`would_drop` 409) if the proposal would silently drop a co-owner's declared method/param; (2) the human
  **severity confirm** — Promote stays disabled until `reconfig-overwrite-confirm`/`reconfig-remove-confirm` is
  acknowledged (a pure add needs none); (3) C10's two-key promote, unchanged. The token now folds the
  **severity decision** (`token_parts += "<severity>|<sorted will_overwrite>"`) so a recomputed-more-destructive
  promote yields a different token (no-quiet-upgrade). A drift/would_drop **409 now carries the diff** (G6).
- **Scope (synthesis M6):** capability blocks only. CUT/deferred: the aggregate multi-object orchestrator,
  module-identity (G7) + inventory host-var (G3) reconfigure (Phase 4b), and secret-field reconfigure (no new
  secret-reconfigure surface is exposed, so M-R-B's NAMES-only will_overwrite is a Phase-4b/secret-rotate
  prerequisite, not landed here). No new write VERB (upsert/current_values/diff are pure) — `WRITE_VERBS`
  unchanged (P0b); the new reads are pinned read-only.
- Covered by `tests/unit/test_diff.py` (every kind + the severity-max + the canonical decision),
  `test_reconfig.py` (verify_no_drop fail-closed + the read-only pins), `test_blockwrite.py` (the upsert),
  `test_backup.py`/`test_observe.py`/`test_logsvc.py` (the wall→reconfigure switch + the token), integration
  (the diff on propose, the drift-with-diff 409), and `tests/e2e/test_reconfigure_flow.py` (the diff renders +
  the confirm gates Promote, a pure add does not). `testid_reference.md` + `gui/README.md` + `SECURITY.md` synced.

### feat(gui): one declarative knob renderer + the Stage-3 render-table — expose every lever (2026-06-19)
Phase 3 of the GUI-paradigm plan (#137) — the goal-2 **exposure** half ("if there are knobs and levers, I want
them exposed and actionable"). Generalizes the old enum-only `capParamsUI` into ONE data-driven renderer fed by
ONE server-side projection, so "expose a knob" becomes a descriptor edit, never UI code (the no-bespoke-config
tenet applied to the GUI). Built on **P0a** — every widened widget is re-validated server-side; the widget is
convenience, the descriptor is the contract, the server is the guard.
- New read-only `kontroll.service.configurable.configurable_view(kind, id)` (`GET /api/configurable`) — the
  uniform **knob-group projection**: a `capability:<cap>` wraps the read-only `suggest_view` + adds a `params`
  knob group per method and the resource_stage group; a `secret-domain` projects its fields group (NAMES only,
  never a value). The knob descriptor is a strict **superset** of the shipping method-param + secret-form shapes,
  so existing descriptors map with **zero edits** (type inferred `allowed`→`enum`). Closed dispatch over the two
  known kinds — NOT a universal "configure-anything" registry (synthesis M5). Pinned read-only (assert_read_only).
- New client renderer (`gui/templates/index.html`, top-level pure → `page.evaluate`-testable): `renderKnob`
  (type→widget table: enum→`<select>`, bool→checkbox, int→number(min/max), text→text(pattern), secret→write-only
  password), `renderSecretKnob` (the existing secret widget extracted — `data-field`/textarea/gen, NEVER
  pre-filled, C11), `renderKnobGroup` (the **render-table** dispatching by `source_kind`), `knobSelection`
  (harvest by `data-param` — the `{method, params}` payload is byte-identical to the old `capSelection`).
- **Closes the specced-but-unbuilt Stage-3 gap:** telemetry's `resource_stage` (`source_kind: curated_list`) now
  renders its descriptor-supplied `cap-telemetry-dashboards` container — with an **honest empty state** (no
  `suggested_dashboards` data yet; that's a later phase), not fabricated chips. `none` (backup/logging) renders
  nothing, as before.
- **Both dialogs converged onto the one renderer:** the capability dialog and the secrets dialog now both fetch
  `/api/configurable` and render via `renderKnobGroup` — a net deletion of two bespoke render loops, with the
  secret dialog's write-only/`data-field`/`secret-apply` contract **unchanged** (the secret e2e is green
  untouched). The write spine is untouched: propose/promote still POST `/api/capability/<cap>`; secrets still
  POST `/api/secrets/<domain>` and STAGE `proposed/<run_id>` (C10).
- Covered by `tests/unit/test_configurable.py` (the projection + the no-value/secret-flag invariant + the
  read-only AST pin), `tests/integration/test_gui_api.py` (the route for both kinds + the 404s), and
  `tests/e2e/test_knob_renderer.py` (the type→widget table, the render-table dispatch, the harvest, and the C11
  "a secret is never pre-filled" check) + a `test_capability_flow.py` assertion that the Stage-3 container
  renders. `testid_reference.md` + `gui/README.md` + `SECURITY.md` C9 synced. No new write verb (read-only
  surface) — `WRITE_VERBS` unchanged (P0b).

### feat(service): typed server-side knob validators — the config-injection guard, widened (2026-06-19)
P0a of the GUI-paradigm plan (#133) — the M3/S-1 prerequisite that **gates** Phase 3's widened knobs. Before
Phase 3 can render anything wider than a `<select>` (an int, a hostname, a free-form text), the server must be
able to re-validate those values; otherwise a widened widget — or a hand-crafted POST bypassing the widget —
could smuggle a metacharacter-bearing value into a rendered `module.yml` block (the config-injection hole the
closed allow-list closed). **The widget is convenience; the descriptor is the contract; the server is the guard.**
- New `kontroll.service._validate` — a per-knob-`type` VALIDATOR registry (the server-side twin of Phase-3's
  client type→widget table): `enum` (the unchanged closed allow-list), `bool`, `int` (`range.{min,max}`),
  `ipv4` (`ipaddress.IPv4Address`), `hostname` (RFC-1123-ish labels), and `text` (a **required** `pattern`,
  `re.fullmatch`'d so a trailing metacharacter/newline can't ride past a loose pattern). `validate_value(knob,
  value)` dispatches on `type` (inferred: `allowed`→`enum`, so **legacy descriptors validate with zero edits**).
- **Fail-closed by construction:** an unknown `type`, or a `text` knob with **no** `pattern`, is REJECTED — a
  descriptor must opt INTO validation, never out of it (design 21 §10's DO-NOT-BUILD line). Validators degrade to
  an error string, never raise/`sys.exit` (the service-never-crash posture).
- `_blockwrite.validate_params` (the shared telemetry/logging Stage-1 param guard) now dispatches each value
  through `_validate.validate_value` — so the widened types reach the exact loop the GUI capability writes use,
  with **no behaviour change** for today's enum-only descriptors (the existing telemetry/logging/backup paths are
  byte-identical). No GUI change in P0a.
- Covered by `tests/unit/test_validate.py` (each type with its injection vector — a trailing `;`, a trailing
  newline, a wrong type, an out-of-range bound — plus the fail-closed unknown-type/no-pattern cases) +
  `test_blockwrite.py` (the widened type flowing through the method-param loop). `SECURITY.md` C9 synced.

### feat(gui): a configurable Homepage editor — arrange the portal from :8443 (2026-06-19)
Phase 2 of the GUI-paradigm plan (#136) — the operator's "configurable homepage": a header **"Homepage"** button
opens an editor to **choose which control-plane tiles appear** and **reorder sections + tiles**. The `:3000`
portal (gethomepage) only READS YAML, so the authenticated `:8443` GUI rewrites the overlay
`instance/dashboards/homepage/{services,settings}.yaml`.
- New `kontroll.service.homepage.py`: `read_board()` parses the two files into one normalized board
  (sections[]+items[]+order); `build_homepage_plan(board)` re-serializes it back via a **LINE-SPAN rewrite**
  (comment- and fleet-marker-preserving — never `yaml.safe_dump`, which would strip the operator's comments),
  computes a `difflib` diff vs current + a `plan_token`; `promote_board(board, token)` token-gates the write.
  Served at `GET /api/homepage` (read) + `POST /api/homepage` (propose → diff+token / apply → staged).
- **Safe by construction:** the serializer only RELOCATES or OMITS spans it parsed from the current file — it
  never synthesizes a tile body, and `_validate_board` rejects any section/tile name not already present, so no
  href/token can be injected (C11). The generated `gen-homepage` fleet block is an opaque, operator-immovable span
  (the #124 banner-ownership idea applied to tiles). The write **stages `proposed/<run_id>`** (C10) — the GUI
  never promotes (no promote button); the proposal shows up in the Pending panel. `apply_homepage_plan`/
  `promote_board` added to the `#133` read-only AST pin (P0b); `read_board` pinned read-only.
- MVP: **item-selection + move-up/down reorder**; HTML5 drag-drop, tile add/edit, and the widgets/bookmarks
  editors are deferred follow-ons (the line-span serializer graduates to `ruamel` when add/edit lands).
- Covered by `tests/unit/test_homepage.py` (the round-trip-IDENTITY safety net, reorder/omit/section-reorder,
  the validate-rejects-a-forged-tile guard, idempotent apply, the token-drift refusal, the AST read-only pin) +
  integration (GET board, propose-purity) + an `e2e` (the editor opens with sections/controls + the staged-result
  render). `testid_reference.md` + `gui/README.md` + `SECURITY.md` C10 synced.

### feat(gui): a read-only "Settings" panel — your instance at a glance (2026-06-19)
Phase 1 of the GUI-paradigm plan (#135, [docs/reviews/2026-06-19-gui-paradigm/99-synthesis.md]) — the smallest,
zero-write-risk slice: a header **"Settings"** button opens a read-only, value-free snapshot of the platform
configuration so the operator can see "what is my instance" without shelling into the box.
- New read-only `kontroll.service.settings.read_view()` — a PURE four-group read (identity: mgmt_ip/domain/
  tls_mode/source_of_truth from `instance.yml`; backup: `backup_remotes`; fleet: `enabled_modules`; status: the
  privileged-mutation arming posture from the GUI's own `KONTROLL_STAGE_PUSHES` env, the running services via
  `list_services`, and the secret-domain roster by NAME with a set/unset badge). Served at `GET /api/settings`.
- **Read-only + secret-safe by construction:** stages nothing, serves NO secret value (the roster is NAMES +
  set/unset only, derived from key presence — never a decrypt; C11), never reads `docker/.env` (C11/C12). The
  arming switch is SURFACE-only (never a toggle — a settings page that could disarm itself is a C10 footgun). Each
  group degrades independently and `read_view` never raises (service-never-sys.exit). The #133 AST pin asserts
  `read_view` is read-only-by-construction (P0b).
- The EDIT/GUARD write surfaces (mgmt_ip re-IP, tls flip, fleet removal) are a later, separately-reviewed,
  C10-staged phase (#140) — Phase 1 is SURFACE only.
- Covered by `tests/unit/test_settings.py` (the four groups, arming reflects env, the NAMES-only roster, per-group
  degrade, the AST read-only pin) + an `e2e` case (the panel opens to all four groups + the arming badge live).
  `testid_reference.md` + `gui/README.md` synced.

### feat(gui): a read-only "Pending" panel — proposals awaiting promotion (2026-06-19)
Operator-requested "list pending operations to promote" (#129, B2 of the GUI planning run). Companion to the A1
run_id fix: a standing list of every staged `proposed/<run_id>` proposal, for when the operator comes back later
and the propose dialog is long gone.
- New read-only `kontroll.service.pending.list_pending()` — a PURE read of the **canonical bare repo**
  (`/srv/kontroll.git` or `$KONTROLL_CANONICAL_GIT`) via a single `git for-each-ref refs/heads/proposed`, returning
  each proposal's `run_id` / sha / staged-at / author / subject / changed-path **NAMES** (never file contents — no
  C8 weight). Missing/empty/non-git canonical → `proposals: []` + an informational note (never raises). Served at
  `GET /api/pending` (read-only, no audit).
- A header **"Pending"** button opens a modal listing each proposal with a **copy-run_id** affordance + a STATIC
  Semaphore `promote-proposal` instruction. **The panel never promotes** — promote stays the separate, admin-authed
  Semaphore app (C10 two-key); there is deliberately no Promote/Approve/Reject control (no deep-link either —
  scope-2). New testids `pending-open`/`-overlay`/`-panel`/`-close`/`-body`/`-error`/`-empty`/`-row`/`-copy-runid`/
  `-paths`/`-promote-hint`.
- The C10 read-only guarantee is pinned two ways: the shared AST pin (#133) asserts `list_pending` calls no
  write/actuation verb, AND a docstring-stripped source grep asserts the module names no git-write subcommand.
- Covered by `tests/unit/test_pending.py` (parse over a throwaway bare repo seeded with `proposed/*` refs;
  changed-paths; empty/absent canonical degrade; the C10 pins; template wiring) + an `e2e` case (the panel opens to
  an honest empty state live). `testid_reference.md` + `gui/README.md` + `docs/privileged-mutation-enablement.md`
  (C10 cross-ref) synced.

### feat(gui): the Fleet panel becomes a two-section Index — services + onboarded fleet (2026-06-19)
Operator-requested "onboarded device/service index" (#128, B1 of the GUI planning run). The #121 Fleet panel was
devices-only; it now leads with a **Control-plane services** section so the operator sees AND jumps to what
kontroll itself stands up (onboard-GUI/Semaphore/Homepage/Grafana/Prometheus/API), then the unchanged onboarded
device-classes.
- New read-only `kontroll.service.fleet.list_services()` — a PURE read of **`config/services.yml`** (SHIPPED +
  repo-tracked, NOT an instance overlay: the set is a product property) with each URL templated from the
  instance's `KONTROLL_MGMT_IP`/`KONTROLL_DOMAIN` env (the same source the capability Grafana deep-links use). An
  unresolved base → `url: None` (name without a dead link); a missing/malformed file → `[]` (never raises).
  Served at `GET /api/services` (read-only, no audit, like `/api/fleet`).
- The header button label widens **"Fleet" → "Index"**; the `fleet-*` testids + the `/api/fleet` contract are
  **unchanged** (label ≠ testid — no churn). The panel renders two independently-degradable sections; new testids
  `services-body`/`services-error`/`services-empty`/`service-row`/`fleet-classes`. No `enabled_when` gating
  (operators omit a service they don't run from the data file).
- **Shared AST read-only pin** (`tests/unit/_readonly_pins.py`, #133): a robust `assert_read_only(module, func)`
  that parses the AST and fails if a "read-only" function calls any write/actuation verb or opens a file for
  writing — replacing brittle source greps; `list_services` is pinned by it.
- Covered by `tests/unit/test_services.py` (env-templating, no-base → None, missing/malformed → [], the shipped
  file lists the core set, the AST read-only pin) + `tests/unit/test_fleet.py` (the two-section template wiring) +
  an `e2e/test_onboard_flow.py` case (the Index opens and lists Semaphore live). `testid_reference.md` + `gui/README.md`
  synced. The per-host backup drill-in lands with its viewer in C1 (#131) — no dead control here.

### feat(dashboards): a real control-plane Homepage on first boot + the login-posture doc (2026-06-19)
Public-readiness (#72) leak: a fresh `:3000` showed example stubs — every tile pointed at RFC-5737 TEST-NET
(`192.0.2.x`) / `example.com`, so a brand-new portal read as broken/leftover. The shipped
`instance.example/dashboards/homepage/services.yaml` is rewritten to the **real control-plane tiles** kontroll
stands up (onboard-GUI `:8443`, Semaphore `:3001`, Grafana `:3002`, Prometheus `:9090`, API `:8444`, NPM
`control.<domain>`), keyed off two `__MGMT_IP__`/`__DOMAIN__` sentinels (#127, A2 of the GUI planning run).
- `kontroll-init --fresh --mgmt-ip X --domain Y` now fills those sentinels at scaffold time (a sibling of the
  existing `instance.yml` personalizer — literal token replace, comment-preserving, idempotent). A fresh node's
  portal shows a working board on first boot; omit a flag and the sentinel survives for a hand-edit.
- **Login posture documented (not changed):** a new `instance.example/dashboards/homepage/README.md` states
  Homepage is *unauthenticated, read-only, no-actuation, mgmt-bound (C3), never WAN* by design; SECURITY.md C3
  gains the cross-ref (no new control row — no trust boundary moves, the served YAML is secret-free). A `Fleet`
  group is reserved between generator markers for a forthcoming `scripts/gen-homepage.py` (B3).
- Covered by `tests/unit/test_kontroll_init.py` (sentinels filled, no placeholder survives, the real URLs present;
  re-run is clobber-safe). `settings.yaml` layout renamed `Management` → `Control plane`. `docs/SETUP.md` +
  `docs/install-from-scratch.md` note the real-tiles scaffold.

### fix(gui): surface the run_id + staged ref on a capability promote (2026-06-19)
The live "where do I get the run_id?" blocker. A capability promote STAGES `proposed/<run_id>` (C10) exactly like
onboarding, but `capPromote` rendered only `PROMOTED + committed: <paths>` — dropping **both** the `run_id` and the
staged ref, so the operator had no id to enter in Semaphore's `promote-proposal` survey to actually land the change.
The server already returned `run_id`/`target_ref`/`staged` (`gui/app.py`); the client just ignored them (#126).
- Extracted a top-level `renderCapPromote(r)` that mirrors `renderOnboard`'s staged/run_id line: `STAGED proposal
  proposed/<ref> (run_id <id>)` on an armed VM, `committed to main (run_id <id>)` on un-armed dev — plus the next
  step ("enter run_id … in the Semaphore promote-proposal task, then run the enact commands").
- Covered by a new `tests/e2e/test_capability_flow.py` case that calls `renderCapPromote()` in the browser with a
  synthetic staged + committed response and asserts the run_id and the `proposed/<ref>` are present.
  `testid_reference.md` `cap-result` row updated.

### feat(backup): retention + destination knobs on the backup capability dialog (2026-06-19)
Operator-requested "levers and knobs": the backup capability dialog exposed only a schedule. It now offers two
more — modeled as **Stage-1 method params** (closed allow-lists), the same pattern logging uses for its
retention, so the generic param picker renders them with **zero template edit** (#123).
- **`retention`** (`keep-all` | `30d` | `90d` | `365d`, default `keep-all`) — written into the `backup:` block,
  carried by `gen-backup` into the schedule spec, and passed to the backup run as `kontroll_backup_retention` by
  `configure-semaphore`. The captures dir is a LOCAL-only git history, so the actual history-prune **enforcement**
  is a deferred follow-up (#125, a destructive git rewrite); a finite window declares intent + flows to the run.
- **`destination`** (`local` | `offsite`) — `offsite` is **offered but refused at propose** with a clear reason:
  config captures aren't encrypted at rest, so an offsite copy needs the encryption-at-rest story first
  (SECURITY.md C8). Fail-closed, never a silent local fallback.
- Both knobs are carried only when declared, so every shipped class keeps a byte-identical `schedules.generated.yml`
  (the `--check` lockfile stays green). Covered by `test_backup.py` (offered knobs, written, offsite refused) +
  `test_gen_backup.py` (carried only when declared). `testid_reference.md` + `capabilities/backup.yml` synced.

### feat(gui): a Fleet panel — see what you've onboarded + jump to its dialogs (2026-06-19)
Operator-requested feature gap: the GUI was search→onboard + capability dialogs only, with **no page listing
onboarded devices**. A header **"Fleet"** button now opens a read-only panel (#121).
- New read-only service `kontroll.service.fleet.list_fleet()` — a PURE read of `instance/fleet.yml` +
  `modules/<key>/module.yml` + `instance/inventory/*` that returns the onboarded fleet **class-centric**: one
  entry per enabled device-class with its declared capabilities (telemetry/backup/logging, from the module's
  `metrics:`/`backup:`/`logs:` blocks) and the inventory hosts in its group. Served at `GET /api/fleet`.
- The panel renders one row per class; each **capability badge reuses the same `openCapabilityDialog(cap, key)`**
  the search cards use, so the operator can actuate telemetry/backup/logging per class straight from the Fleet
  view (declared **or** addable — the dialog detects). Read-only, so it never gates onboarding (INVARIANT D*).
- Covered by `tests/unit/test_fleet.py` (class→host join; capabilities mirror only declared blocks; missing-module
  skip; the template wiring). `testid_reference.md` adds the Fleet-panel testids.

### docs(install): the promote→enact itinerary + the §5/§6 order fix (2026-06-19)
Closes the deferred half of the blind-human install fixes in `docs/install-from-scratch.md` (#120), all
live-sourced from the dogfood box:
- **§5/§6 renumber (M1):** *Deploy the stack* is now §5 and *Semaphore project* (`configure-semaphore.py`) is §6,
  matching the real run order (configure talks to the Semaphore the deploy just brought up) — no more "run §5
  before §6" inversion warning.
- **Token already minted (M4):** §7 states the C10 `kontroll_api_token` is **already** generated by
  `kontroll-init --fresh` into the `dashboards` domain — the GUI Secrets form is for *rotation*, not first mint.
- **Arm all three (M5):** §7 arms `["api","onboard-gui","semaphore"]` with `api_privileged=true` in **one** deploy
  (and provisions the onboard-gui age key first), so all three privileged surfaces are recreated together.
- **Promote = a separate Semaphore app (B4):** §7 spells out the two-key promote — `https://<mgmt>:3001`, login
  `admin`/`semaphore_admin_password` (NOT the GUI password), the `promote-proposal` template, the `run_id` survey.
- **The ordered propose→promote→enact itinerary (B5):** §8 is now a numbered onboard → (capability) → promote →
  enact walkthrough, with the Tier-1/Tier-2 enact split and the Grafana deep-link.

### feat(gui): a Grafana deep-link from the telemetry capability dialog (2026-06-19)
Operator-requested: after a telemetry propose→promote, the capability dialog now surfaces a **clickable Grafana
link** so you jump straight to the device's dashboards instead of hunting for the URL (#122).
- `observe.build_telemetry_plan` emits a generic `links: [{label, url}]` — an "Open Grafana" base plus a
  `/d/<uid>` deep-link for each curated dashboard. The uid is read from the **committed** dashboard JSON
  (`dashboards/grafana/dashboards/<name>.json`, where fetch-dashboards pins it) so the link is reproducible; the
  base is `http://<KONTROLL_MGMT_IP>:3002` (the by-IP Homepage form) or the reverse-proxied
  `https://grafana.<KONTROLL_DOMAIN>`. No base resolvable ⇒ no links (never a fabricated host).
- The capability route (GUI + API) passes `links` through **generically** (any capability may emit them — zero
  spine branch); the dialog renders them as a `cap-links` anchor block on propose (preview) and after promote.
- The onboard-gui container is now passed `KONTROLL_MGMT_IP`/`KONTROLL_DOMAIN`. Covered by
  `test_observe.py::test_grafana_links_deep_link_to_committed_uid`; `testid_reference.md` adds `cap-links`.

### fix(onboard): reuse an existing device-class, don't overwrite/strip it (2026-06-19)
Live-caught on the dogfood box (main): onboarding a device whose CLASS already exists (e.g. `cisco_ios`, a shipped class) wrote
a MINIMAL module that collided, and the operator's Overwrite then **replaced the rich curated module — silently
stripping its `metrics:`/`backup:`/`logs:` blocks** and breaking the per-class backup auto-enroll (#124). Now
onboarding an existing class **REUSES** it:
- `build_onboard_plan` detects when `modules/<key>/module.yml` already declares the onboarded collection and sets
  `module_reuse` — adopting that curated class's `role`/`backend`/`secrets_domain`/`inventory_group` so the host
  attaches faithfully; `apply_onboard_plan` then **skips the module write entirely** (the curated blocks survive)
  and adds only the inventory host + fleet-enable.
- The drop-in inventory host now **merges** (new `gitio.write_inventory_host`, banner-keyed ownership): a 2nd
  device of a class is added alongside the 1st instead of clobbering it. A FOREIGN file (operator-rewritten, no
  banner) still hard-stops with `WriteConflict`.
- A genuinely DIFFERENT collection colliding on a key is NOT reuse — it still hard-stops; Overwrite remains the
  escape hatch but the GUI now fires a **loud `confirm()`** warning that it can strip a curated class's capabilities.
- Plan views (GUI + API) carry `module_reuse`; the CLI + GUI dry-run render a "REUSING it" note. Covered by
  `test_onboard_dryrun.py` (reuse keeps the module; 2nd-host merge; different-collection still conflicts).

### feat(onboard): an "Overwrite" escape hatch on a drop-in collision (2026-06-19)
Follow-on to the 409 conflict fix: when an onboard apply hits a divergent existing module/host drop-in (e.g. a
device-class key colliding with a shipped module), the GUI now offers an **"Overwrite the existing file"** button
instead of just a dead end. `gitio._write_new` gained an `overwrite` flag (skip the WriteConflict and replace the
file — the operator's deliberate choice, logged `! overwriting divergent …`); `apply_onboard_plan(plan,
overwrite=)` threads it; the GUI 409 carries `conflict: true`, and the front-end renders an `onboard-overwrite`
button that re-submits with `overwrite=true`. The API's `OnboardIn` gained the same `overwrite` field. Covered by
`test_write_conflict.py` (overwrite replaces the file; the button + conflict-flag wiring) + `testid_reference.md`.

### feat(gui): onboard-form polish — collapsible peek + a remote-conditional push box (2026-06-19)
Two operator-requested UX gates on the onboard form, found while exercising it on the fresh VM:
- **Collapsible "just looking" form.** The `onboard-toggle` now flips its label to "Collapse" when open and
  **disables itself while the form holds input** (`onbPristine`: any of group/host/username/password/api_token/
  ssh_key filled, or any box ticked). A form opened only to peek can be dismissed, but real input is never
  silently dropped by a stray re-click.
- **`also push origin (offsite backup)` is now conditional.** It renders only when the server reports an OFFSITE
  git remote (`gitio.offsite_remote_exists()` → `has_remote`, passed to the template). A fresh node has no offsite
  remote, so `git push origin` would just no-op/fail — the box is now hidden there instead of misleading. The
  submit body guards the now-optional checkbox (`push` defaults false when absent).
- Tests: `tests/unit/test_onboard_form_gates.py` (the `offsite_remote_exists` predicate across no-origin / local /
  offsite + the template wiring of both gates); `testid_reference.md` updated.

### fix(onboard): a drop-in collision returns a clean 409, never a worker-killing crash (2026-06-19)
Live-caught driving the GUI on the fresh VM: onboarding a device with `key=cisco_ios` (a SHIPPED module) made the
GUI return a bare **"request failed"** (`HTTP 000`, the connection dropped). Root cause: `gitio._write_new`
signalled "refusing to overwrite a divergent drop-in" with **`sys.exit(...)`**. In the CLI that's fine, but the
GUI/API call the onboard service IN-PROCESS — and `sys.exit` raises `SystemExit`, which derives from
`BaseException`, so Flask/Werkzeug does NOT catch it: it killed the request worker with no response. (The codebase
already states the rule — `service/_blockwrite.py`: "a service must degrade, not crash the worker".)
- `gitio._write_new` now raises a catchable **`gitio.WriteConflict`** instead of `sys.exit`; the message tells the
  user to pick a new device-class key.
- The onboard routes (`gui/app.py`, `api/routes/onboard.py`) catch it → **clean HTTP 409**; the CLI (`galaxy.py`)
  catches it at dispatch → clean exit (old UX preserved). Pinned by `tests/unit/test_write_conflict.py` +
  `test_onboard_dryrun.py` (updated to assert WriteConflict). **Live-verified:** the exact failing onboard now
  returns `409 "modules/cisco_ios/module.yml already exists with different content — pick a new device-class key"`.

### fix(install): install-prereqs waits for the apt lock (cloud-init first-boot race) (2026-06-19)
A clean fresh-VM **re-verification** of the corrected flow (PR #28 fixes baked into the bundle) found the one
remaining §0 snag: a freshly-booted cloud node is still running cloud-init's first-boot `apt`, so
`install-prereqs.sh` — run immediately, exactly as the docs say — raced the lock and aborted under `set -e`
(`Could not get lock /var/lib/apt/lists/lock`). Added an apt `DPkg::Lock::Timeout "600"` drop-in so it **waits**
instead of racing; a stranger who SSHes in and runs it right after boot just works. **Re-verified the full
§0–§8 install end-to-end on a pristine VM, zero manual steps:** prereqs → `kontroll-init --fresh` → fleet
ssh-key → `deploy-stack` (self-generated the lockfile, `ok=41 failed=0`) → GUI (`401`) → `configure-semaphore`
(project wired: 4 operational + 5 backup templates). The deploy-stack self-gen fix (PR #28) is confirmed on a
real fresh node.

### fix(install): a live human-perspective walkthrough makes a fresh install actually completable (2026-06-19)
A blind-human install audit (`docs/reviews/2026-06-18-blind-human-install/`, an ultracode workflow simulating a
stranger following only the docs) returned **NO → YES-WITH-FIXES**: the machinery works, but the *prose* failed a
cold reader and one **real fresh-node deploy blocker** existed that the automated #71 dogfood never hit (it was
driven by someone who knew the tricks). Walked live on a stock Debian 13 VM; fixes applied + validated:
- **deploy-stack self-generates the runner collection lockfile (CODE — the hard blocker).** The runner image
  COPYs `ansible/collections/requirements.generated.yml`, a generated + gitignored artifact absent on a fresh
  node, so the image build failed `"…requirements.generated.yml": not found`. deploy-stack regenerates every
  *other* config-as-data artifact before bring-up; the lockfile was the one omission. Added the regen task
  (`gen-requirements.py`, the same single-source generator) right before the runner-image build, gated identically
  — so a fresh install needs **no prior `bootstrap.yml`**. Live-validated: lockfile removed → deploy regenerates +
  builds clean. Pinned by `tests/unit/test_deploy_stack_requirements.py`.
- **install-from-scratch.md prose fixes (the stranger-blockers):** **B1** a root `START-HERE.md` + README
  re-pointed to the bundle path (the entry point was undiscoverable); **B2** §6 is the **bare**
  `deploy-stack.yml` (the doc's `-e @/tmp/sv.yml` is a phantom GUI-enact artifact that fails `file not found`);
  **B3** added the `ssh-keygen ~/.ssh/ansible_ed25519` step (configure-semaphore hard-crashes without it, no doc
  generated it); **B6** documented provisioning the `onboard-gui` age key (fail-closed, host-provisioned — the GUI
  is not in the §6 default set); **M2** §6 service list trimmed to the two that come up first; **M3** stated
  bootstrap is subsumed by deploy-stack; **M6** the GUI login is `gui_admin_password` (was mislabeled
  `GUI_PASSWORD`); **M7** a how-to-choose `--mgmt-ip`/`--domain` callout; **M8** a show-once-secrets warning + the
  SOPS recover path; **M9/M10** the edit-the-overlay how-to + a §2 "nothing to do here during install" banner.
- Live-verified on the dogfood box: stack up (semaphore/homepage), GUI up (`GET / → 401`, authed `/api/search → 200`).
  Deferred (a focused follow-up): the §7/§8 promote/enact prose (B4 "promote is a second web app", B5 the enact
  itinerary, M4/M5, the §5/§6 renumber).

### feat(onboard): unified root SSH-auth seam — onboarding accepts a key OR a password, anywhere (2026-06-18)
The operator's G6 decision: any SSH path universally accepts username/password **OR** a key, at the **root seam**,
not per-capability. Scoped via an ultracode blackboard (`docs/reviews/2026-06-18-ssh-auth-unification/`,
GO-WITH-FIXES, all 7 must-fixes folded in). Built as a **strict mirror of the already-shipped password path** —
**zero backend edits, zero per-capability SSH-auth code** (auth is purely an inventory concern, so every SSH
consumer — the device_role backends, the wiring roles, backup/ping — inherits the key the same way it inherits the
password). **Backward-compatible:** the key branch is purely additive; password/token hosts are byte-unchanged.
- **Onboard** (`scripts/kontroll/service/onboard.py`): a 4th `if ssh_private_key` branch emits
  `ansible_ssh_private_key_file` (a `{{ kontroll_device_key_dir }}/<host>.key` path) + a NON-secret
  `kontroll_ssh_key_domain` hostvar (key hosts only); the PEM is normalized (CRLF→LF + trailing newline for
  paramiko) and SOPS-stored per host. `apply` now writes creds via **`sops_write_domain` (STDIN, whole-domain
  decrypt→merge→write)**, never `sops_set` (argv) — the PEM never rides a `ps`-visible command line (MF-3).
- **GUI**: a `field-ssh-key` `<textarea>` on the onboard form (paste a PEM — an alternative/fallback to the
  password); threaded through the GUI + API routes.
- **Materialization** (the net-new piece): a pre-run **`_render-ssh-keys.yml`** (via a fleet-only
  `_fleet-preamble.yml` imported by ping/backup-configs/wire-logging/install-node-exporter) decrypts each key
  host's key from SOPS to a `0600`, `no_log`, run-uid-owned file in a per-run dir **outside the repo**, in
  WHICHEVER runner executes (Semaphore-correct: the job holds `SOPS_AGE_KEY` + sops/age); **`_cleanup-ssh-keys.yml`**
  removes it last. A key-auth host whose domain the runner can't decrypt **fails LOUDLY**, never a silent no-key.
- **Scope decision (B):** the scoped Semaphore key is **not** widened to `compute`, so a `docker_host` SSH key is
  control-node-only (its Semaphore backup keeps the shared bootstrap key) — preserving the deliberately-bounded
  Semaphore breach radius. **`file_tail_ssh`** stays a documented key-only least-privilege exception.
- **Materialization correctness (two bugs the scratch-VM dogfood caught, both invisible to CI/unit tests):**
  (1) every `_render-ssh-keys.yml` / `_cleanup-ssh-keys.yml` task now `delegate_to: localhost` — a fleet host's
  `ansible_connection` (network_cli/ssh/httpapi, set by its backend+group_vars) **overrides** a play's
  `connection: local` keyword, so without delegation the LOCAL decrypt/copy ran over the DEVICE connection (a
  switch can't run `file`/`copy`) and the key never materialized for ANY real host. (2) the key copy now uses a
  literal block (`content: |`) + `| trim`: the `community.sops` lookup strips the decrypted file's trailing
  newline, dropping the KEY's own final newline on `from_yaml` reparse — OpenSSH then rejects the 1-byte-short
  key (`error in libcrypto`). Both pinned by static regression tests.
- Tests: `test_onboard_ssh_key.py` (path/domain emitted, PEM never in the inventory, CRLF normalize,
  PEM-via-stdin-not-argv, render-`no_log`, loud-fail, **delegate_to: localhost on every render/cleanup task**,
  **literal-block + trim trailing-newline guarantee**) + e2e `test_onboard_flow.py` (the textarea); SECURITY.md
  accepted-risk row; `testid_reference.md`.
- **Dogfood (proof bar met):** on the control VM a key-auth host onboarded with a SOPS-stored key was reached by a
  fleet `ping` over SSH using ONLY the materialized key (`IdentitiesOnly=yes` → `"ping": "pong"`); the 0600 key
  materialized + cleaned up, and a `-v` run showed **zero PEM fragments** in the log (`no_log` held).

### feat(gui): close the log-surface credential gaps — file_tail_ssh key + the cap-dialog cred prereq (2026-06-18)
The two design-needing gaps from the public-readiness audit (G2/G3), so the **log** surface reaches the same
pure-GUI parity observe has:
- **G2 — the `file_tail_ssh` SSH private key is now GUI-supplyable.** New `secret-forms/logging_file_tail.yml`
  (an `ssh_private_key` field, `type: textarea`) + the secret dialog now renders a **`<textarea>`** for
  multi-line secrets (a single-line `<input>` can't hold a PEM). The operator no longer has to hand-`sops` the
  key. The keypair lifecycle (mint, authorize the public half on the target via the wiring role, pin host keys)
  stays out-of-band by design — the GUI doesn't model SSH-keypair minting (keygen is age-only).
- **G3 — the capability dialog now surfaces a method's credential prerequisite.** `offerable_methods` (logging +
  telemetry) now carry `secret_domain`; when the picked method names one, the dialog shows an **advisory,
  non-gating** pane (`cap-cred-prereq`) — "⚠ needs the `<domain>` credential before it goes live" + a one-click
  "supply it in Secrets" jump (`cap-cred-open`) — mirroring the onboard `provisioning:` pane. Closes the silent
  divergence where a declared log method never told the operator a SOPS cred was required.
- Tests + testids same-commit: `test_logging_methods.py` (offerable methods carry `secret_domain`),
  e2e `test_capability_flow.py` (the cred-prereq surfaces) + `test_secrets_flow.py` (the key renders a textarea);
  `testid_reference.md` rows for `cap-cred-prereq`/`cap-cred-open` + the textarea note.

### chore: public-readiness audit + close the easy-win gaps (2026-06-18)
A read-only ultracode audit (`docs/reviews/2026-06-18-public-readiness-audit/`) verified the two public-cut theses
against the *current code*: **blind-joe** (GUI-only + unique-values-only onboarding) = **PARTIAL** (control + observe
+ secrets hold to a staged proposal; the log surface had real GUI gaps) and **no-bespoke** (every non-unique vendor
fact auto-sourced or pinned+validated) = **HOLDS** (zero bespoke-no-source facts; the one tail was 5 `:latest`
images). **No blockers.** The honest framing it pinned: the GUI goes *to a committed + staged proposal*, not *to
live* — enact stays a deliberate post-promote Semaphore/deploy step (C9/C10). The operator-chosen easy-wins:
- **G1 — the logging capability is now reachable in the GUI.** `gui/templates/index.html` `CAP_DIALOGS` omitted
  `'logging'`, so its badge rendered **inert** though the whole log backend/route/dialog were built+tested. Added
  `'logging'` (one line) + an e2e badge-click test (`test_capability_flow.py`) + the `testid_reference.md` row.
  Observe **and** log now both ride the one shared dialog in the GUI.
- **G4 — pinned the 5 floating `:latest` images** (no-bespoke: an exporter's port/path facts are only exempt when
  the image is pinned): `prometheus:v3.5.4`, `grafana:13.0.2`, `homepage:v1.13.2`, and the two **generated**
  exporter fragments via their descriptors — `blackbox-exporter:v0.28.0` (`telemetry/blackbox.yml`),
  `prometheus-pve-exporter:3.9.0` (`telemetry/pve.yml`), regenerated. Now nothing rides a floating exporter image.
- **G5 — the `logging_rest` poll token is GUI-supplyable** — added `secret-forms/logging_rest.yml` (one token
  field); the per-vendor injection wiring lands when a class adopts `rest_pull`.
- **G7/G8 — doc truth:** corrected `api-architecture.md` (no kontroll route fires a Semaphore task-run as-built —
  the trigger is planned) and the stale "backup joins at Phase 8" comment (backup + logging are both live).
- Deferred (need a design call, recorded in the synthesis): G2/G3 (the `file_tail_ssh` SSH-key GUI path + a
  capability-dialog credential-prerequisite pane), G6 (confirm password-SSH suffices), G9 (full-walk SNMP validation).

### docs(security): formalize the logging-ingress residuals (4+5) + record the un-defer blueprint (2026-06-18)
Ahead of the public cut, the two remaining C12 logging residuals are formalized to a public-release-ready posture and
the full hardening seam is **scoped** (an ultracode blackboard: 3 research → 2 design → 1 adversary,
`docs/reviews/2026-06-18-logging-ingress-hardening/`) — but **not built** (the mgmt-bound instance has no live
exerciser; building unexercised crypto plumbing is the anti-pattern). The build is a triggered follow-up; the
reviewed `99-synthesis.md` is its contract.
- **Stale mechanism corrected:** SECURITY.md said the syslog-ingress hardening was a "per-device **PSK**" (`:563` +
  the `:426` `no_log` narrative). Vector's `syslog` source has **no PSK mode** — the only auth it offers is **mTLS
  client certs from a shared control-issued CA**. Both spots fixed.
- **Residual 4 (syslog `:5514`)** → hardening = opt-in **mTLS over a shared CA** (forged lines rejected at the
  handshake), per class, plaintext-fallback for senders that can't TLS, fail-closed-at-generate. Un-defer trigger:
  reachable beyond the mgmt VLAN.
- **Residual 4b (journald-upload `:19532`)** → the unauthenticated half is **BLOCKED ON UPSTREAM** (systemd#4092
  open: `--listen-https` does not enforce client certs; Debian dropped `--trust=`), not merely deferred. Interim:
  prefer the TLS-syslog path for Linux hosts. Un-defer trigger: systemd ships OpenSSL client-cert verification.
- **Residual 5 (Loki no-auth)** → hardening = an opt-in **basic-auth Caddy gateway** (`loki_auth: off|basic`, off =
  strict no-op; Loki stays single-tenant behind it), **not** `auth_enabled` (tenancy ≠ auth). The first of the three
  to un-defer. Trigger: Loki `:3100` reachable beyond mgmt VLAN, or the C9 SSO residual closes.
- The blueprint bakes in the adversary's 7 must-fixes — notably **MF-1**: the CA private key must sign ONLY on the
  control plane, never in a Semaphore wiring role (else a Semaphore breach could mint a client cert for any device).
- Doc-only; no behaviour change. The review dir + synthesis are committed as the build contract.

### feat(validate): grant-coverage A′ — the provisioning grant must cover a perm-gated read (2026-06-17)
The heavier role→privilege-map A′ (deferred when `schema_permission` found the pinned pull `/cluster/tasks` is
`{user: all}` — nothing perm-gated to anchor). A live apidoc probe found the real anchor: `/cluster/status` GET
requires a hard `Sys.Audit` check, exactly what the provisioning surface's **PVEAuditor** grant confers and the
proxmox metrics path depends on — and nothing validated it (`token_scope` only exercises the ungated pull). Closed
in two halves on `logging/proxmox_api.yml`, reusing the shared `*.Audit` token:
- **`schema_permission` generalized** with an optional `api_path:` override → pins that the apidoc still REQUIRES
  `Sys.Audit` on `/cluster/status` (catches PVE relaxing/changing the gate — least-privilege drift).
- **New `check_grant_covers`** → LIVE-reads the perm-gated endpoint with the descriptor's own token: `200` ⇒ the
  grant confers the gate's permission; an **unambiguous `403`** ⇒ the grant lost coverage and the privileged reads
  break (no `200+empty` corroboration needed — a *gated* endpoint 403s cleanly, unlike the `{user: all}` pull).
- The role/permission stay descriptor DATA (`perm:`/`expect:`); the checks judge only schema/HTTP-status, never a
  literal (MF-4 preserved — `PVEAuditor` lives once, in the provisioning block). This closes the loop between the
  onboard provisioning grant (PR #19) and the validate seam.
- **Tests:** `tests/unit/test_gen_validate_live.py` (grant_covers OK/DRIFT/UNREACHABLE + token-never-leaks; the
  `api_path` override OK/DRIFT). Live-verified read-only against the real PVE (2026-06-17). SECURITY.md C13 updated.

### chore: pre-public cleanup sweep — bound the ansible run-log (G5), pin caddy, bump flask (2026-06-17)
Loose ends cleared ahead of the public cut; none changes behaviour for the running stack:
- **G5 closed — the ansible run-log is now bounded.** `ansible.cfg` `log_path` appends with no built-in rotation
  (unlike the audit `RotatingFileHandler`), so the persistent run-log grew unbounded. `deploy-stack.yml` now drops
  `/etc/logrotate.d/kontroll-ansible-log` (`size kontroll_ansible_log_max_mb`×`5M` default, `rotate
  kontroll_ansible_log_max_files`×`5` default) and ensures the `logrotate` package. **`copytruncate` is
  load-bearing** — Vector tails the run-log by *exact* path (`internal_ansible.yaml`), so rename-rotation would
  orphan its tail on the old inode; copytruncate keeps the inode. Pinned by `tests/unit/test_ansible_log_rotate.py`.
- **Pinned the Caddy default image** `caddy:2` → `caddy:2.11.4` (consistent with the `loki`/`vector`/`snmp-exporter`
  patch-pins — a reproducible default). The acme path still overrides `KONTROLL_CADDY_IMAGE` with the operator's
  xcaddy+DNS build, so the stock default never runs in practice.
- **Bumped flask** `>=3.0` → `>=3.1.3` (dependabot #3, replicated here so it lands on a current base — #3's CI failed
  only on its stale branch point).
- *Noted, not changed:* the `grafana`/`prometheus`/`semaphore`/`homepage` images still float `:latest` (a broader
  pin pass, out of scope here).

### fix(logging): de-root the journald-remote receiver — close the C12 residual 3 root half (2026-06-18)
The `systemd-journal-remote` receiver (the mgmt-bound `:19532` journald-upload landing) ran **root** — a
network-facing unauthenticated listener as root. Now it runs as the package's own non-root `systemd-journal-remote`
user (`Group=systemd-journal`, + `NoNewPrivileges`/`ProtectSystem=strict`/`PrivateTmp`), writing a `2750` setgid
output dir so its `.journal` files inherit `group=systemd-journal` — the **same supplementary gid** non-root Vector
reads host journald by, so one `group_add` covers both reads (interlocks with PR-A). The **unauthenticated** half
(plaintext upload) stays the deferred residual 4. Pinned by `test_journald_receiver.py`; SECURITY.md row + the role
README mark the root half closed.

### fix(logging): pin the file_tail_ssh host key — close the C12 TOFU (2026-06-18)
The exec-ssh log-pull source TOFU'd the target host key (`StrictHostKeyChecking=accept-new` into a *writable*
known_hosts), so a first-connect mgmt-VLAN MITM could substitute a target's key undetected. Now `=yes` against an
**operator-pinned, read-only** `known_hosts` assembled by deploy-stack from `device_trust.<key>.ssh_known_hosts_file`
(a sibling of the device-CA bundle — same always-written/G9 pattern), RO-bound into Vector. An unpinned/changed key
makes ssh **refuse** (fail-closed; the stream blanks) rather than silently accept. **Fail-closed surfacing (MF-4):** a
`file_tail_ssh` class produces no logs until the operator pins the key — deploy emits a non-gating warn when the
assembled known_hosts is empty; `instance.example/instance.yml` documents the `ssh_known_hosts_file` schema.
Keyscan-at-provision only *relocates* the TOFU (it's unauthenticated), so it's an operator-reviewed helper, never an
automated trust step (D-D). Pinned by `test_file_tail_ssh.py`; SECURITY.md C12 row → TOFU closed.

### feat(logging): Vector non-root + docker-socket-proxy — lift the C12 promotion blocker (2026-06-18)
The one logging residual the mgmt-VLAN baseline can't cover (it's post-compromise blast radius): Vector ran **root**
holding the root-equivalent Docker socket, so a Vector RCE = `POST /containers/create` = host root — which **blocked
promoting the logging stack to the live control VM**. Now:
- **No raw socket in Vector** — a new gated `docker/services/docker-socket-proxy.yaml` (`tecnativa/docker-socket-proxy`,
  `POST=0` ⇒ GET/HEAD-only) alone mounts `/var/run/docker.sock`; Vector's `docker_logs` targets it over the kontroll
  net. The root-equivalent surface is gone (the proxy cannot create/exec/kill a container).
- **Non-root Vector** via the compose `user: "10002:10002"` (the universal switch — covers the stock + gated image;
  uid distinct from Loki's 10001). journald read via `group_add: ${KONTROLL_JOURNAL_GID}` (the host `systemd-journal`
  GID, discovered by deploy-stack `getent`). The file_tail_ssh key + dir render owned by the Vector uid (SSH demands
  it). **MF-1 (the adversary's catch):** the audit dirs are widened to `o+r` rather than `group_add: 1001`, which
  would have over-granted read of the `0640 1001:1001` API/GUI TLS private keys.
- Pinned by `test_compose_logging.py` (non-root, socket-proxy, proxy-read-only, uid-distinct-from-Loki, no-gid-1001)
  + `test_file_tail_ssh.py` (Dockerfile non-root, key-uid == compose uid) + the data-dir-bind guard. Scoped via the
  ultracode blackboard `docs/reviews/2026-06-18-logging-hardening/` (GO-WITH-FIXES). **GATE A PASSED (scratch-VM
  dogfood, 2026-06-18):** non-root Vector (uid 10002 + the systemd-journal GID) shipped 1822 host journald events +
  container logs through the proxy (0 errors); `POST /containers/create` via the proxy → 403. The dogfood also
  caught that the `vector-data` data_dir must be a 10002-owned BIND (a named volume inits root-owned → non-root
  Vector couldn't write its checkpoint) — fixed. **Promotion unblocked.**

### feat(validate): A′ — the schema-permission check (online-validate fast-follow, 2026-06-18)
The deferred A′ from the online-validate scope, RESHAPED by a live finding. The original A′ ("the pinned endpoint
still requires the permission our grant provides") was **moot for our actual dependency**: a live apidoc inspection
showed `/cluster/tasks` GET is `{"user": "all"}` — any authenticated token may call it; scope is enforced by
per-row **audit filtering**, not an endpoint permission (which is exactly why a too-narrow token gets 200+empty,
not 403 — the M-8 trap `token_scope` already handles). So A′ became a **permission-MODEL stability** check:
`check_schema_permission` parses the node-local apidoc and confirms the endpoint's permission shape still equals the
pinned `expect:` (`{user: all}`), **DRIFTing** if PVE tightens it to a hard `perm` check — the deterministic,
pre-wire warning that `token_scope`'s row-filter assumption no longer holds.
- One more `check:` in the same vendor-blind `_VALIDATORS` table; the expectation is descriptor DATA
  (`logging/proxmox_api.yml validate: {check: schema_permission, expect: {user: all}}`), no role NAME hardcoded
  (MF-4). Read-only apidoc GET — no new surface (rides C13). Hermetic tests (match/tighten/absent/unavailable);
  the `{user: all}` expectation was apidoc-verified against the real PVE read-only.

### feat(validate): online drift-validation — the no-bespoke tenet's VALIDATE leg (2026-06-17)
The offline `gen-* --check` scripts prove a pinned vendor fact is internally consistent; this is the twin that
proves it still matches the LIVE device. `scripts/gen-validate-live.py` reaches each enabled class's hosts
read-only and confirms the facts the consumers depend on haven't drifted on the wire: the TLS posture
(`cert_posture`, B2 online), the pulled PVE API path (`api_path`), the token's read scope (`token_scope`), and a
device's SNMP OID support (`snmp_oid_support` — a 2nd transport, proving the dispatch is generic, not HTTPS-only).
- **Drop-in, vendor-blind:** a top-level `validate:` sibling list on the descriptor that already owns the fact
  (`modules/proxmox`, `logging/proxmox_api.yml`, `telemetry/snmp.yml`) — dispatched over a `_VALIDATORS` function
  table keyed by `check:` NAME with **zero vendor branch** (a `tests/validate.sh` grep-gate machine-enforces a
  literal-free spine).
- **Reach-then-judge:** OK | DRIFT | UNREACHABLE | PENDING-HARDENING — a transport error is UNREACHABLE (a down
  host is tolerated), a successful mismatch is DRIFT (loud, non-zero exit); a device that hardened past its pinned
  self-signed posture is PENDING-HARDENING, not a cry-wolf. The 200+empty audit-filter trap is disambiguated via a
  `/version` corroboration GET (never a silent OK).
- **Read-only, no new surface (SECURITY.md C13):** an outbound client — no new listener, no new credential (reuses
  the audit-scoped proxmox token + the SNMPv3 USM user), a closed GET/handshake/GETNEXT transport allow-list. The
  token is an `${ENV}` ref resolved only at the fetcher boundary; the audit line carries NAMES only (C12). Tier =
  **control-VM / on-demand, never hermetic CI** (it reaches the lab); the dispatch + judgement are proven
  hermetically via an injected fake fetcher (`tests/unit/test_gen_validate_live.py`).
- Composes with the onboard provisioning-surface (below): one declaration, two consumers — the surface PREVENTS a
  silent mis-provision, `token_scope` DETECTS one on the wire. **Live-verify pending** on a scratch VM (the real
  PVE read-only). Scoped via `docs/reviews/2026-06-17-online-validate-seam/`.

### feat(onboard): provisioning-surface — credential prerequisites SURFACED at onboarding, not buried (2026-06-17)
The blind-joe constraint (onboarding = IP + creds, never a hand file-edit) leaked one residual: a device class can
need a manual cred step beyond "give me a token" — e.g. the PVE token must carry the `PVEAuditor` role — and that
was buried in a source comment. A class now declares those prerequisites as SHIPPED data
(`modules/<key>/module.yml` `provisioning:` — a list of `{grant, note, docs_url?}`), and the onboard plan surfaces
them. Generic (any class, not proxmox-bespoke) and strictly **non-gating** (INVARIANT D*).
- **Reader:** `catalog.module_provisioning(collection)` SCANS `modules/*/module.yml` matching `collections[].name`
  — NOT a path-join by key (at onboard time the handle is the collection slug, e.g. `community.proxmox`, not the
  shipped key `proxmox`). Homed in `catalog` so the validate seam's token-scope/role check reads the same `grant`.
- **Threaded** best-effort into `build_onboard_plan` (after the error gates, like the telemetry nudge) — a reader
  failure degrades to `[]`, never gates onboarding; the block is NEVER written into the onboarded module (the
  `_ONBOARD_KEYS` pin holds). Projected NAMES-only in both `_onboard_plan_view` twins (GUI + API).
- **GUI:** a sibling `<pre data-testid="onboard-provisioning">` advisory node — `Run` stays enabled regardless
  (the operator can onboard with a wrong scope; the validate seam detects it on the wire). Closes punch-list B3.
- Tests: `test_catalog_provisioning.py` (resolve-by-collection-not-key) + `test_invariant_d.py` (surfaces +
  survives a reader failure) + the proxmox-shaped e2e. A public role NAME is public vendor vocabulary — no secret,
  no trust boundary moves.

### refactor(observability): snmp_exporter if_mib — GENERATED from public MIBs, not hand-authored (2026-06-17)
The next no-bespoke-config tenet application: the snmp_exporter `if_mib` module (interface counters/status +
sysUpTime, scraped agent-less from the Cisco switch / FortiGate) was the last hand-authored vendor-fact config —
its OIDs were typed by hand into `prometheus/exporters/snmp/snmp.yml.j2`. It is now DERIVED from public IETF MIBs
(IF-MIB RFC 2863 + SNMPv2-MIB RFC 3418, vendored under `prometheus/exporters/snmp/mibs/`, pinned to net-snmp v5.9
+ IANA) via a committed `generator.yml` → `scripts/gen-snmp.py` (the pinned snmp_exporter generator v0.30.1) →
`prometheus/exporters/snmp/modules.generated.yml` — the same committed-artifact + `--check` shape as every other
`gen-*`. **Behaviour-preserving:** the generated module reproduces the EXACT 6-metric / 7-OID scrape (same OIDs,
types, single ifName lookup, kept ifIndex label) — pinned by `tests/unit/test_gen_snmp.py` against a frozen oracle.
- **Secret boundary preserved:** the committed artifact is `modules:` only (public OIDs); the SNMPv3 USM creds stay
  SOPS-templated into the gitignored 0600 `snmp.yml` (`no_log`) via `snmp.yml.j2`'s `auths:` block +
  `{% include 'modules.generated.yml' %}`. `gen-snmp.py` strips the generator's verbatim-copied `auths:` (no leak).
- **Pinned in lockstep:** `prom/snmp-exporter:latest` → `:v0.30.1` (generator version must == exporter version); the
  regenerated `docker/services/snmp-exporter.generated.yaml` + `test_gen_exporters.py` updated in the same change.
- **Two-tier `--check`:** `gen-snmp.py --check` (binary-free, in `tests/validate.sh`) + a dedicated CGO
  `snmp-modules` CI job (rebuild from the vendored MIBs with the pinned generator, `git diff --exit-code`). The
  generator is NetSNMP/CGO, so regeneration is CI/VM-only (see `prometheus/exporters/snmp/README.md`). Designed +
  adversarially reviewed via the blackboard run (`docs/reviews/2026-06-17-snmp-generator/`). **Live-verify pending
  on a scratch VM:** the `{% include %}` resolution + a runtime smoke (which also produces the authoritative
  committed artifact for the byte-gate).

### feat(observability): pve-exporter TLS — the B2 telemetry twin (one vendor fact, both consumers, 2026-06-17)
Finishes B2's dedup. `telemetry/pve.yml`'s `PVE_VERIFY_SSL: "false"` (the IDENTICAL vendor fact `proxmox_api`
already derives) is no longer hardcoded — `gen-exporters` derives it from the consuming proxmox class's
`vendor_defaults.tls_posture` via the SAME `scripts/kontroll/endpoints.py` resolver (descriptor declares
`exporter.tls_verify_env: PVE_VERIFY_SSL`, no literal). **The PVE self-signed fact now lives in ONE place, derived
by BOTH consumers** (logging pull + telemetry exporter) — no drift. Generated fragment byte-unchanged
(self_signed → "false"); the instance CA pin (`device_trust.proxmox.tls_ca_file`) flips it to "true" symmetric
with logging; fail-closed (no/conflicting class posture → exit). Pinned by `test_gen_exporters.py`.

**CA-mount completion (the pinned-CA hardening path, now end-to-end).** Closes the gap the twin surfaced: a pinned
CA was *emitted* into config but never *mounted*. Now `device_trust.<key>.tls_ca_file` is a repo-relative HOST
path to a PUBLIC CA PEM; deploy-stack **assembles every pinned CA into ONE bundle** (`{{kontroll_vector_dir}}/
device-ca-bundle.pem`, 0644, empty placeholder when none — the G9-safe static bind), and BOTH consumers verify
against it: Vector's `source.tls.ca_file` → the mounted bundle (`endpoints.VECTOR_CA_BUNDLE`, static vector.yaml
bind); the pve-exporter → `REQUESTS_CA_BUNDLE` + a generated `:ro` mount (research-verified: `PVE_VERIFY_SSL` is
bool-coerced, can't be a path). The generators write the **fixed container target**, never the operator's host
path (the host→container split). One bundle, no per-class hub edit. Pinned by `test_endpoints.py` (the bundle
constants) + the updated logging/exporter CA-pin tests; SECURITY C12 + the registry doc + `instance.example`
synced. **Remaining follow-up:** the online `--check` drift-probe (live cert posture + `/cluster/tasks` vs the
PVE API schema; VM-needing).

### feat(observability): proxmox_api TLS — derived from a public class fact, not bespoke (B2, 2026-06-17)
The first application of the **no-bespoke-config tenet**: a device/service-specific value must be GENERATED from a
public source + pinned + validated, never hand-bespoked, so churn → a reviewable diff. `proxmox_api` hardcoded
`tls.verify_certificate: false` — the SAME vendor fact `telemetry/pve.yml`'s `PVE_VERIFY_SSL` also hardcodes (a
duplicated, unpinned leak).
- **Vendor FACT, captured once:** `modules/proxmox/module.yml` gains `vendor_defaults: {tls_posture: self_signed}`
  (PVE ships a self-signed cert by default — public PVE fact, pinned + visible). The SHARED resolver
  `scripts/kontroll/endpoints.py` maps posture → verify; both the logging pull and (next) the telemetry exporter
  derive from this ONE fact, so they can't drift.
- **De-hardcoded:** `logging/proxmox_api.yml` drops the literal `tls:` block + opts in via `tls_from_class: true`;
  `gen-logging` injects `source.tls` from the class fact ⊕ the instance decision. Generated output byte-unchanged
  (self_signed → `verify_certificate:false` == the retired literal — derive, don't drift).
- **Instance DECISION (hardening, now WIRED — not a TODO):** `instance.yml` `device_trust.<key>.tls_ca_file` pins
  a CA → the generator flips verify ON + emits `ca_file` (the operator's trust call, distinct from the vendor fact).
- **Fail-closed:** a `tls_from_class` method whose class declares no/an unknown posture exits non-zero (never
  silently `verify=false`). Pinned by `tests/unit/test_endpoints.py` + `test_gen_logging.py`; SECURITY.md C12 row
  reframed (derived pinned default + wired CA-pin, not an unconditional accepted risk).
- **Follow-ups:** wire `telemetry/pve.yml`'s `PVE_VERIFY_SSL` to the same resolver (the twin leak — one call); the
  online `--check` drift-probe (live cert posture + `/cluster/tasks` vs the PVE API schema); B3 (token-scope doc).

### feat(logging): file_tail_ssh — remote journald PULLED over SSH (S11, 2026-06-17)
The outbound (reach-out) twin of the journald receiver: instead of the host pushing (S10), **Vector PULLs** a
remote Linux host's journald over SSH — for hosts where you can authorize a read key but won't run an uploader
unit or open an inbound `:19532` socket. The aspirational `file_tail_ssh` local-file method (no module ever used
it) is resolved to this, the operator's chosen journald-over-SSH model.
- **Mechanism:** Vector's `exec` source runs `ssh -i <key> root@<host>`; the host's `authorized_keys` FORCES
  `journalctl -f -o json` with `no-pty`/no-forwarding (`roles/logging_file_tail_ssh`), so the key runs ONLY that
  read command — no shell, no arbitrary file read. Vector decodes the NDJSON, shaped by the SAME
  `._HOSTNAME`/`._SYSTEMD_UNIT`/PRIORITY derivation as `journald_remote` → Loki (`source=capability`).
- **Bench-caught (the dogfood earning its keep again):** the **stock Vector image has no ssh client** (only
  `journalctl`). So `file_tail_ssh` declared ⇒ deploy-stack builds + selects a thin **`kontroll/vector`** image
  (stock + `openssh-client`, `docker/vector-image/Dockerfile`, FROM pinned in lockstep with `vector.yaml`) via
  `${KONTROLL_VECTOR_IMAGE:-<stock>}`; every other instance keeps the stock pinned image (gated, like the S10
  receiver). `local/s11-file-tail-ssh-bench-findings.md`.
- **`logging/file_tail_ssh.yml`** → `kind: exec` (ssh + `${DEVICE_ADDR}`), `secret_domain: logging_file_tail`.
  **gen-logging** `_subst` now recurses into list/dict so `${DEVICE_ADDR}` substitutes into the exec command argv.
  **The read key** (the 3rd injection shape — a FILE, not an env): deploy-stack renders the `logging_file_tail`
  SOPS private key `no_log` to a `0600` file + **vector.yaml** bind-mounts it RO at `/etc/vector-keys/file_tail_ssh`
  (an empty placeholder keeps the bind valid until onboarded). Sender forced command flipped `-o short-iso`→`-o json`.
- **Pinned:** `test_gen_logging.py` (exec-ssh shape + the `_subst` list recursion); `test_file_tail_ssh.py` (the
  gated image + FROM-lockstep, the RO key mount ↔ descriptor `-i` path, the deploy gate, the no-shell forced cmd).
- **C12:** SECURITY.md gains the `file_tail_ssh` accepted-risk row (read key at rest in Vector + host-key TOFU,
  mgmt-VLAN-only); `instance/.sops.yaml` + secrets README document the `logging_file_tail` domain (base recipients).

### feat(logging): journald-remote collector receiver — journal-remote + Vector exec journalctl (S10, 2026-06-17)
The inbound (receive) render-half of the logging capability, BENCH-PROVEN end-to-end (a first attempt as a Vector
`http_server` was built, live-DISPROVEN, and reverted — PR #12/#13 — before this correct mechanism; the live dogfood
caught what CI couldn't). A remote Linux host ships its journal to the collector:
- **Mechanism (live-proven on a scratch VM → Loki):** remote `systemd-journal-upload` → `systemd-journal-remote` on
  the mgmt IP `:19532` (writes `/var/log/journal-remote/*.journal`) → Vector **`exec`** source
  `journalctl --directory=/var/log/journal-remote -o json -f` → shaped (`._HOSTNAME`→host, PRIORITY→level,
  `._SYSTEMD_UNIT`→service) → Loki, labeled `source=capability`. WHY exec and not http_server (binary export format,
  not JSON → 0 events) or a Vector `journald` source (filters to current boot `+0`, drops remote journals) — both
  live-disproven; findings in `local/s10-journald-receiver-bench-findings.md`.
- **`logging/journald_remote.yml`** → `kind: exec` (the journalctl command). **`roles/logging_journald_receiver`**
  (new, collector-side): installs `systemd-journal-remote` + a `kontroll-journal-remote.service` bound to the mgmt
  IP only (`--listen-http={{ logging_collector_host }}:19532`, never `0.0.0.0`), writes a SEPARATE
  `/var/log/journal-remote` (not under `/var/log/journal` → no double-ingestion by `internal_journald`).
- **deploy-stack** provisions the receiver ONLY when a class declares `journald_remote` (gated on the rendered
  drop-in). **vector.yaml** bind-mounts `/var/log/journal-remote:ro`.
- **Pinned (codifies the bench learnings):** `test_gen_logging.py` (the source is `exec` journalctl json — never
  http_server/journald, never `current_boot_only`); `test_journald_receiver.py` (mgmt-IP bind never `0.0.0.0`,
  the separate dir, the descriptor↔receiver path agreement); `test_compose_logging.py` (the bind mount).
- **C12:** SECURITY.md gains the `:19532` accepted-risk row (unauthenticated mgmt-bound; receiver runs root).
  The host-side wiring role `logging_journald_remote` (sender) was already correct.

### feat(observability): UNIFY secret→env injection across telemetry + logging — `secret_env_map` (2026-06-17)
The operator's follow-up to `auth_injection` (below): is the REST of deploy-stack's `.env` secret render modular, or
still bespoke? It wasn't — the telemetry exporter creds (`PVE_EXPORTER_*`) were hand-listed in `deploy-stack.yml`,
their `telemetry/pve.yml::secret_env_map` "documentary today", and even the logging loop depended on a hardcoded
`_sops_domains: {proxmox, snmp}` map (a new domain still needed a spine edit). A 4-agent ultracode design-audit
(`docs/reviews/2026-06-17-secret-injection/`, GO-WITH-FIXES) found the elegant collapse and the must-fixes; this
builds it.
- **One unified shape:** `secret_env_map: {ENV_VAR: "<str.format template over the secret_domain's SOPS field
  NAMES>"}` in BOTH `telemetry/` and `logging/` (retires `auth_injection` — the general name wins). A 1-field
  exporter map (`PVE_EXPORTER_USER: "{proxmox_api_user}"`) is the degenerate `"{field}"` case of an N-field logging
  token, so the shipped `kontroll_render_token` filter renders BOTH with **zero filter change**, no `kind:` branch.
- **One generator → one manifest:** new `scripts/gen-secret-env.py` scans every enabled module's `metrics:` +
  `logs:` methods' `secret_env_map` into ONE value-free `config/secret-env.manifest.generated.yml` (NAMES only;
  `--check`-gated; `auth_injection_files` lifted out of `gen-logging.py`; the old `logging-auth.manifest` orphan-
  pruned). Fail-CLOSED on a bad env name / template field / a **platform-core collision** (a device descriptor can
  never shadow `SEMAPHORE_*`/`KONTROLL_API_TOKEN` — the loop renders after the platform-core literals) — gates
  `gen-exporters.py` never had.
- **deploy-stack: ONE generic render loop + generic decrypt-by-name** that KILLS `_sops_domains` + the per-domain
  `_proxmox`/`_snmp` lookups + the hand-listed `PVE_EXPORTER_*` lines. The new `kontroll_sops_domain` filter
  decrypts the UNION of referenced domains BY NAME (the domain == the file stem), **fail-soft PER domain** (a fresh
  node without a domain onboarded still deploys, that env empty) and **lazy / `no_log`-only** (no `set_fact` of a
  decrypted dict). A new credentialed exporter OR log-pull is now a `secret_env_map` drop-in — **zero deploy-stack
  edit**; proxmox/snmp/fortigate are named NOWHERE in the spine.
- **M4 (full):** deploy-stack now resolves the secrets dir through the SAME overlay seam (`paths.py resolve`) the
  GUI onboarding write-path uses — no hardcoded `instance/secrets` that could drift under a relocation. Also fixes
  the latent `snmp`↔`snmp_observability` key mismatch the `_sops_domains` deletion exposed (the snmp config-FILE
  render stays a separate task — the third injection shape, intentionally not folded into the env loop).
- **Platform-core boundary (decided + enforced):** intrinsic control-plane creds (`SEMAPHORE_*`/`GRAFANA_*`/`GUI_*`/
  `KONTROLL_API_TOKEN`) stay explicit **fail-CLOSED** `.env` lines (they don't grow with the fleet); device/consumer
  creds are the **fail-SOFT** data-driven loop. The rule: *does this secret grow with the fleet?* — a collision test
  enforces it.
- **Render parity (M5):** the loop's empty-on-absent now comes from the filter (falsy→`''`) not Jinja `default('')`
  (undefined→`''`); equivalent for a real string token, pinned so the semantics shift is deliberate. 8 covering
  checks + the filter + collision + per-domain fail-soft tests, each with a what+why docstring (same commit). Docs:
  `telemetry/README`, `logging/README`, SECURITY C12, `.env.example`, observability-onboarding-flow, agent-less.
- **B1 live-verify (dogfood-PASS) + a latent bug it caught:** the ACTUAL `deploy-stack.yml` render run on a fresh
  throwaway control-node (real ansible/sops, DUMMY SOPS domains) composed every line correctly — the N-field logging
  token, the 1-field telemetry exporter env (the SAME loop), and the platform-core lines — proving decrypt-by-name +
  the unified loop + M4 end-to-end. It ALSO caught that `ansible/ansible.cfg` declared no `filter_plugins` path, so
  Ansible couldn't find `kontroll_render_token`/`kontroll_sops_domain` (they live in `ansible/filter_plugins/`, not
  the playbook-adjacent default) — a runtime-only failure that affected this AND the merged PR #10 token render,
  never exercised live until now. Fixed (`filter_plugins = filter_plugins`, the existing `roles_path`/`collections_path`
  style) + a `test_ansible_filter_discovery.py` drift guard that pins the cfg path + every `| kontroll_*` filter the
  playbooks use is registered there.

### feat(logging): data-driven credentialed-token injection — `auth_injection` (closes the bespoke gap) (2026-06-16)
The pull-modularity audit's one real finding: the `KONTROLL_PVE_LOG_TOKEN` injection was hand-wired, Proxmox-shaped
code in deploy-stack + vector.yaml (a 2nd credentialed method needed 3 hub edits). This makes it DATA — adding a
credentialed pull method is now a pure drop-in.
- A method declares `auth_injection: {token_env, token_template}` (the env var the `source.auth.value` references
  + a `str.format` over its `secret_domain`'s SOPS field NAMES). `gen-logging` emits a **value-free manifest**
  `config/logging-auth.manifest.generated.yml` (keyed by `token_env` → its domain + template; **no token value**).
- deploy-stack's `.env` render loops over the manifest and composes each token via a new `kontroll_render_token`
  filter plugin (`str.format` over the decrypted domain, **fail-soft to `''`** if any field is absent — preserves
  the prior "empty token when un-onboarded" behaviour), under `no_log`. The hand-wired proxmox token COMPOSITION
  is **deleted**; the proxmox-named compose test became a generic "every credentialed method's `token_env` is
  passed through" (no method named). (The Vector env passthrough stays a one-line `${VAR:-}` entry in
  `vector.yaml` — compose `include` rejects merging an addition into an existing service — carrying no secret/shape.)
- **Fail-closed (C12):** `_validate_auth_injection_or_exit` requires a credentialed method (pull + `source.auth` +
  `secret_domain`) to declare `auth_injection`, and pins `token_env` to the `${...}` the `auth.value` references
  (single source of truth). Filter + generator + compose tests added; the token VALUE never lands in a committed
  file. Result: a FortiGate/OPNsense credentialed pull is a `logging/<m>.yml` drop-in + a one-line secret-free
  `vector.yaml` passthrough, zero deploy-stack edit, no token SHAPE in any hub.
- Scope (operator decision): logging-only. The identical deferred telemetry gap (`secret_env_map`, Phase-3) is
  NOT folded in here — it can ride the same mechanism later.

### feat(logging): data-driven pull fan-out — `pull_unnest` + `dedupe_key` pre-transforms (2026-06-16)
A pull log method can now emit ONE deduped Loki event per array element, declared as DATA (no generator branch).
Two optional descriptor fields: `pull_unnest: <array-field>` renders a pre-transform `. = unnest!(.<field>)`
(`is_array`-guarded) that fans a `{data:[...]}` poll to one event per element; `dedupe_key: <field-path>` renders
a Vector `dedupe` transform that drops an entry already ingested on a prior poll (a recent-window endpoint has no
since-cursor). `gen-logging._render_fragment` threads a `prev_id` so the fragment becomes a chain
`src → [unnest] → [dedupe] → shape`, each optional stage rendered only when its field is present — a method
declaring neither renders the identical 2-component fragment as before (the no-branch generality gate stays green).
- **proxmox_api** now declares `pull_unnest: data` + `dedupe_key: data.upid` + a per-task `transform_vrl`
  (`.data.type`→service, `.data.node`→host, `.data.status`→level, `.data.upid`→run_id, `encode_json(.data)`→body),
  so the PVE `/cluster/tasks` stream becomes one deduped event per task instead of a coarse hourly batch.
- **Fail-closed (C12):** `_validate_pull_shape_or_exit` rejects a metacharacter-bearing field (the unnest/dedupe
  paths are an injection surface — closed dotted-identifier allow-list) or either field on a non-pull method.
- **Modularity audit (the trigger):** an ultracode blackboard (`docs/reviews/2026-06-16-pull-modularity/`) verified
  the generator + capability spine + blind-joe flow are free of any Proxmox/method-name code — the endpoint lives
  in the shipped descriptor (maintainer-authored once; a blind joe regenerates it, never hand-edits it). It also
  found ONE real bespoke gap — the `KONTROLL_PVE_LOG_TOKEN` deploy-stack injection is hand-wired (the same gap as
  the deferred telemetry `secret_env_map`); its data-driven fix (`auth_injection`) is designed + tracked, not yet built.
- **LIVE-VERIFIED (full-pipeline dogfood, VM 102 → real PVE `/cluster/tasks` → Loki):** the device-gated VRL
  (Windows can't `vector validate`) was refined against real output. The dogfood caught **three** fallibility bugs
  that only surface at `vector validate` — all fixed GENERICALLY (no method-name branch): (1) `. = unnest(.x)` is a
  fallible assignment (E103) even inside an `is_array` guard, because the guard doesn't narrow the field's type →
  now `. = unnest!(.x)` (the abort is unreachable under the guard); (2) the canonical baseline default
  `string(.field) ?? "default"` becomes an *unnecessary*-coalesce error (E651) when a method's `transform_vrl`
  PROVABLY assigns a string (proxmox's `.level = if … {"info"} else {"error"}`) → all three defaults are now the
  infallible `if !is_string(.field) { .field = "default" }` guard (identical effect, never coalesces, so it
  compiles whether the field is provably-string or maybe-null); (3) the descriptor's `encode_json(.data) ?? "{}"`
  is E651 (`encode_json` is infallible) → dropped the `??`. Result: Vector starts clean; **50 Loki events, 50
  distinct upids (dedupe PASS), all shaped** (`.service` = task type, `.host` = node, `.level` from status). A
  regression test pins the infallible idiom. The `scrape_interval` stays an operator knob (now safe to shorten
  since dedupe is proven).

### feat(logging): credentialed pull — `proxmox_api` method ingests the PVE cluster tasks read-only (S8) (2026-06-16)
The first concrete CREDENTIALED PULL log source: a `logging/proxmox_api.yml` drop-in whose Vector `http_client`
GETs the Proxmox `/api2/json/cluster/tasks` audit history read-only, authenticating with a `PVEAPIToken`
`Authorization` header (`auth.strategy: custom`). The proxmox module declares `logs: [{method: proxmox_api}]`;
gen-logging fans it with **no code change** (the auth/tls blocks pass through `_subst` verbatim).
- **Live-proven read-only (HTTP 200, authorized).** A read-only probe with the real token confirmed the
  PVEAPIToken header format + token scope. It also corrected the endpoint: `/cluster/log` was empty on the idle
  cluster (200/count=0), so the method targets `/cluster/tasks` — the reliably-populated audit stream (every
  VM/backup/migrate task: node/type/status/user/upid/timing). **Follow-on** (gen-logging pull-method
  enhancement, shape now known): an `unnest`+`dedupe(upid)` pre-transform to emit one deduped, canonically-shaped
  Loki event per task; until then the source ships the task array as a coarse hourly batch event (LogQL-queryable,
  `scrape_interval` kept long to bound re-ingestion).
- **Token: reuse, env-only, never committed.** The header token REUSES the existing audit-scoped pve-exporter
  token (no new SOPS domain — it already carries only `Sys.Audit`/`PVEAuditor`, enough to READ the log). It's
  composed from the `proxmox` domain into `.env` by deploy-stack under `no_log` and passed to Vector as
  `${KONTROLL_PVE_LOG_TOKEN}`; the generated config carries **only the `${ENV}` reference, never the value**
  (pinned by tests). Read-only egress — an HTTP GET actuates nothing on Proxmox (C12; SECURITY.md updated, incl.
  the `tls.verify_certificate: false` self-signed-cert accepted-risk row).
- **Pull-source consistency fix.** `gen-logging._render_fragment` now derives a pull source's endpoint address
  and its `.host` label from ONE sorted `(name, addr)` pair — the first real pull source exposed a latent bug
  where two independent sorts (by addr for the endpoint, by name for the label) picked DIFFERENT hosts when the
  orderings disagreed (endpoint `192.0.2.11` but label `my-hypervisor-2`), mislabeling the stream. Pinned by a test.
- **Verified so far / still pending.** The credentialed read (auth header + token scope + endpoint, read-only)
  is **live-proven by a direct `curl`** (HTTP 200). The full Vector PIPELINE end-to-end (a running Vector
  actually polling → shaping → landing in Loki) and the `unnest`+`dedupe` per-task shaping are **not yet run** —
  they ride the follow-on enhancement above. Findings recorded in `local/proxmox-api-live-findings.md`.

### feat(logging): data-driven shaping — `transform_vrl:` per-method field derivation (S9) (2026-06-16)
The capability log pipeline now shapes each method's events from DATA, not a generator name-branch. The rendered
Vector shaping transform is two halves: a GENERIC baseline (structural `.source`/`.device` + the canonical
`.service`/`.host`/`.level` fallbacks) plus the method's own field derivation declared as a `transform_vrl:` list
in `logging/<method>.yml`, spliced in BEFORE the defaults so a real derived value wins.
- **`syslog_push`** maps Vector's RFC5424 `.severity` → canonical `.level`; **`journald_remote`** maps
  `._SYSTEMD_UNIT` → `.service` and numeric `PRIORITY` → `to_syslog_level(...)` — the SAME derivation the live
  internal journald transform uses. Previously the capability path lost a journald event's real unit/level to the
  method-name/`"info"` fallbacks; it now carries them.
- **No `if method == …`:** `gen-logging._render_fragment` never branches on the method name — a new derivation is
  a registry-file edit, not a generator change. The baseline shed its prior syslog-ism (a hardcoded `.severity`
  reference) and is now genuinely method-agnostic.
- **Fail-closed (C12):** `transform_vrl` is shape-validated at generate time (must be a non-empty list of
  non-empty strings — a bare string or non-string entry exits non-zero), so a mistyped descriptor fails before it
  can emit garbage VRL. The VRL body is registry-authored (same trust boundary as `source:`); `vector validate`
  is the semantic gate. Pinned by `tests/unit/test_gen_logging.py` (splice-order, generic-baseline, the real
  journald derivation, malformed-rejection). Live end-to-end (a real syslog/journald event) is device-gated.

### feat(storage): .env storage-location paradigm — KONTROLL_STORAGE_ROOT + the C8 chown-follows-path guard (2026-06-16)
The second half of the operator's storage request (the first was retention/size knobs): pre-provision the
stack's at-rest stores on the disk(s) you choose, via `.env`, without editing compose or playbooks. Built from a
reviewed blackboard (`docs/reviews/2026-06-16-storage-paths/`, GO-WITH-FIXES):
- **5 knobs:** `KONTROLL_STORAGE_ROOT` (default `/var/lib/kontroll`) + the high-growth overrides
  `KONTROLL_LOGS_DIR` (Loki), `KONTROLL_BACKUPS_DIR`, `KONTROLL_API_AUDIT_DIR`, `KONTROLL_ANSIBLE_LOG_DIR`.
  `api/`/`onboard-gui/`/`caddy/` derive from the root (no per-dir knob — the review killed 3 inert ones). Named
  volumes (Prometheus TSDB, Grafana, Postgres, Vector buffers) stay Docker-managed, not relocated.
- **C8-safe (M-2):** one var feeds BOTH the compose bind source AND the deploy-stack chown, at the identical
  value, chown-before-`up` — so a relocated store is created uid-scoped, never a Docker-auto-created `root:root
  0755` bind. **Fail-closed:** every compose source is `${VAR:-/var/lib/kontroll/…}` (empty can't mean `/`), and
  deploy-stack asserts the root is an absolute path other than `/`.
- **New CI guard `tests/check-storage-chown.py`** (a `yaml`-walk of deploy-stack): every storage-root-derived
  bind source must have a uid-scoped provisioner (file/copy chown · git clone · fail-closed `stat`) — an EXACT
  own-path match, no permissive ancestor walk. Registered in `validate.sh` + `validate.ps1`; unit-pinned by
  `tests/unit/test_storage_paths.py`, which **proves the guard fails** on a synthetic un-provisioned store AND if
  the age.key guard is removed (a guard never seen to fail is worthless).
- **Closes two pre-existing G9 holes** as a side-effect: `onboard-gui/age.key` (no task today → a fail-closed
  absence guard; never minted) and `caddy/` (bare `root:root 0755` → uid-scoped `1001:1001 0750`). Vector's three
  RO audit re-reads are repointed to the same vars (else relocation silently stops log shipping).
- Behaviour-preserving: a zero-`.env`-change deploy is byte-identical to today. Doc-sync: `.env.example`,
  SECURITY.md C8, docs/SETUP.md, docs/logging-architecture.md. **Deferred:** the full `KONTROLL_CANONICAL_GIT`
  wiring into local-canonical.yml (the var-default ships now; `/srv/kontroll.git` is already provisioned).

### feat(logging): enact per-class Loki retention — generated runtime_config overrides (S5) (2026-06-16)
The `retention` method param (`7d`/`30d`/`90d`) was validated but **consumed nowhere** — a silent inert knob
(an operator who picked 90d got the 30d global). S5 makes it real, the storage-paradigm bridge of the
blackboard build, on a seam **dogfood-verified live on the pinned Loki 3.4.2**:
- `gen-logging.py` emits **`docker/loki/overrides/retention.generated.yaml`** — a Loki `runtime_config` overrides
  file (hot-reloaded every 10s, **no restart, no hub edit**) whose `retention_stream` selects
  `{source="capability", device="<key>"}` at the chosen window. `loki-config.yml` gains the one `runtime_config`
  hook; `loki.yaml` mounts the dir RO; `deploy-stack.yml` regenerates it (gated `when: 'loki' in stack_services`)
  before bringing Loki up.
- **Fail-closed:** a class without `retention` contributes no override (its streams inherit the bounded global
  `LOKI_RETENTION_PERIOD`); no class with retention ⇒ the file is `overrides: {}`, never empty=infinite.
- **The M-1 dogfood (docs/reviews/2026-06-16-storage-logging/40-*) corrected the design + hardened it:** the
  per-tenant key is **`fake`** (auth_enabled:false), NOT `*` (which would target a non-existent tenant — a silent
  no-op); the period floor is **24h** and an **invalid runtime_config is fatal at Loki startup**, so the generator
  only emits valid ≥24h presets + an always-valid default; a per-tenant `retention_stream` **REPLACES** (not
  merges) the static list, so the generator **carries the static debug/audit baseline forward** (else the 90d
  audit retention silently regressed to 30d — a C12 data-loss). All four corrections are pinned by tests.
- **Test** `tests/unit/test_gen_logging_retention.py`: fail-closed empty default, the `fake` tenant + carried
  baseline + 90d→2160h mapping, every preset ≥24h, the file is always valid YAML, the longest preset ≤
  `reject_old_samples_max_age` (the §3.3 coupling), and conflicting per-class retention is rejected. `gen-logging
  --check` now covers both the Vector tree and the Loki overrides path.

### feat(logging): graceful-export probe — non-gating suggestion enrichment (S6) (2026-06-16)
The "intelligent external sourcing" half of the storage+logging build, shipped as the *buildable-now* plumbing
(the live device read is S7, `needs_real_device`). A read-only probe can SHARPEN a logging suggestion with
evidence — structurally incapable of ever becoming an onboarding gate:
- **`suggest_logging` gains an optional `evidence` arg** (default `None`). With `None` — the only value the
  onboard/promote spine ever passes — the output is **byte-identical** to before (INVARIANT-D preserved by
  construction). When present, the evidence can only re-word the *note* of a candidate the vector+hint ALREADY
  produced; it can **never add, remove, or gate** a candidate (evidence-with-no-candidate is silently ignored).
- **`probe.logging_export_probe(addr, conn, run_show)`** — a READ-ONLY (`show logging` / `show running-config |
  include logging host`), FAIL-SOFT helper: any unreachable/timeout/unknown-command degrades to `{state: maybe}`
  and **never raises**, so a probe failure can't abort an operator action. The per-plugin read command is
  config-as-data in `capabilities/logging.yml` (`probe.show.*`), never logic in `classify.py`.
- **Test** `tests/unit/test_suggest_logging_probe.py`: the `evidence=None` byte-identical regression pin
  (INVARIANT-D), evidence-can't-manufacture-a-candidate, the fail-soft helper (a raising read → `maybe`), and
  the descriptor→callable drift pin. Docs: `docs/observability/logging-capability-vector.md` §1.1.

### feat(storage): fail-closed audit-log rotation knobs (M-5) (2026-06-16)
Promote the hardcoded API/GUI audit-log rotation (5 MB × 5) + the script run-log prune (30d) to operator-tunable
`.env` knobs — the fourth slice of the storage+logging blackboard build, with the fail-closed clamp the review
required (V1 M-5):
- `KONTROLL_AUDIT_MAX_MB` / `KONTROLL_AUDIT_MAX_FILES` (read by `api/audit.py` + `gui/app.py`) +
  `KONTROLL_RUNLOG_RETENTION_DAYS` (`scripts/lib/run-log.sh`). Defaults == the prior hardcodes (behaviour-preserving).
- **M-5 (the must-fix):** the audit log is a C12 security surface — a rotation sized from an env must NEVER
  become `maxBytes=0`/`backupCount=0` (= no rotation = an unbounded audit file). New
  **`kontroll.envguard.positive_int`** clamps empty/unset/non-numeric/below-minimum to the safe default, **never
  0**; both audit surfaces route through it. The run-log prune clamps a non-numeric day count to 30 in shell.
- Wired through `deploy-stack.yml` + the api/onboard-gui container env; documented in `.env.example`.
- **Test** `tests/unit/test_envguard.py` pins the clamp (`"0"`/`"abc"`/empty/`-3` → default, never 0) and that
  both audit surfaces route through it.
- **Deferred (now DONE — see the pre-public cleanup sweep under `[Unreleased]`):** the ansible run-log logrotate
  bound (G5) — a host logrotate drop-in (`kontroll_ansible_log_max_mb`/`_max_files`).

### docs(logging): troubleshooting runbook (the degrade path) — Layer 3 complete (2026-06-16)
The operability surface of the logging system, the third slice of the storage+logging blackboard build:
**`docs/troubleshooting.md`** — the **signal map** (which `source`/`service` label each kontroll surface lands
under), the common **LogQL queries** (kept in sync with the logs dashboard), and the load-bearing **degrade
path**: how to keep debugging when Loki/Vector ITSELF is down (`docker logs` / `journalctl` / the flat audit
files at `/var/lib/kontroll/{api/audit,onboard-gui/.../local,ansible-log}` + `~/.local/state/kontroll/runs`).
Closes the last Layer-3 doc gap (`logging-architecture.md` §6 item 5 → ✅; item 4 dashboards+alerts → ✅). The
logs-as-secret-surface control already lives in **SECURITY.md C12** (Vector-root + plaintext-syslog as
documented accepted risks). Build contract: `docs/reviews/2026-06-16-storage-logging/99-synthesis.md`.

### feat(logging): Layer 3 — logs dashboard + Grafana-managed alert rules (2026-06-16)
The logging system's surface, the second slice of the storage+logging blackboard build — no new container.
- **`dashboards/grafana/dashboards/logs.json`** (hand-authored, auto-loaded by the file provider): volume by
  source, error/warn rate, top services, the 90d audit trail, run-id correlation, and a live log stream filtered
  by source/service/level/run_id + a body `search`. Every panel/target pins the Loki datasource `uid: loki`;
  panels key ONLY off the canonical C12 label set; body search is a LogQL `|~` filter (never a label).
- **`provisioning/alerting/logging-rules.yaml`** + a default no-op contact point — Grafana-managed unified
  alerting (NO separate Loki ruler): container-restart storm, backup-config failure, onboard error, `auth-denied`
  spike — each a numeric threshold + bounded window + `for:` debounce (fail-closed). **Closes
  `logging-architecture.md` G6** (nothing watched the log stream).
- **Tests** (hermetic, no live Grafana): `test_dashboards_logs.py` (datasource-uid pins, closed template-var
  set, run_id scoped to `source="internal"`, no non-canonical label split) + `test_alerting_logging.py` (every
  rule hits loki + bounded window + debounce; the `auth-denied` token pinned as a shared contract per V1 M-9;
  contact point present). Live rendering is verified post-deploy. Docs: `dashboards/README.md` + Layer 3 🟡.

### fix(prometheus): cap the metrics TSDB on disk — close the one real storage fail-open (2026-06-16)
Prometheus had time-based retention (`90d`) but **no disk-size cap** — a runaway scrape or label explosion could
fill the disk and take down the whole control plane. This was the single genuine fail-open hole the
storage-paradigm blackboard found (`docs/reviews/2026-06-16-storage-logging/`); the first slice of that paradigm
closes it and promotes the retention flags to **fail-closed `.env` knobs**:
- **`PROM_RETENTION_SIZE=20GB`** (NEW disk hard cap) + **`PROM_RETENTION_TIME=90d`** (was hardcoded) →
  `--storage.tsdb.retention.{size,time}=${VAR:-default}`. Prometheus deletes a block when it violates **either**
  (whichever trips first). Sized comfortably above steady-state (~1 GB) so it only ever stops a real runaway.
- Rendered by `deploy-stack.yml` + documented in `docker/.env.example`. Behaviour-preserving for existing
  instances (defaults = prior values); empty/unset always falls to the non-empty default, never "unbounded".
- **Test** `tests/unit/test_storage_bounds_fail_closed.py` proves the EMPTY-env case (every BOUND retention var
  non-empty in `.env.example` + every compose consumer uses the `${VAR:-default}` form + the size cap exists) —
  the V1 must-fix that the fail-closed safety is *enforced*, not just *documented*. Paradigm + sequence:
  `docs/reviews/2026-06-16-storage-logging/99-synthesis.md`.

### chore(repo): codify the file-per-agent blackboard as the standard ultracode pattern (2026-06-16)
Ultracode multi-agent runs now use a **reusable runner** instead of a hand-rolled script each time, so the
netcanon discipline can't drift. **`.claude/workflows/blackboard.js`** (invoked via the Workflow tool by
`scriptPath`) bakes in the load-bearing contract: read-only agents each write **EXACTLY ONE** long-form report
under `docs/reviews/<UTC-date>-<slug>/`, read peers' reports for cross-phase comms, and return only a
pointer/summary; the **main thread** writes the `00-blackboard.md` seed up front + the `99-synthesis.md`
reconciliation after, and is the **sole** actor that builds/commits. Parameterized by `args` (phases × agents);
opus by default; tolerant of string-delivered args; loud up-front validation (never inside a `parallel()` thunk,
which would swallow it). Smoke-tested end-to-end. Docs: [.claude/workflows/README.md](.claude/workflows/README.md)
(args contract + invocation) + [docs/agent-workflow.md](docs/agent-workflow.md) (§ Ultracode runs + the seed
template) + the CLAUDE.md "Distributed agent work" pointer.

### feat(capability): logging — device-side wiring roles + syslog ingress + promote-path fix (2026-06-16)
Completes the logging capability's ENACT half so a fresh operator ("blind joe") can take a `logs:` declaration
all the way to logs-in-Loki, not just propose/promote the data. Built on `feat/logging-vector`.
- **Device-side wiring roles** (`ansible/roles/logging_{syslog_push,journald_remote,file_tail_ssh}/`) + the
  thin dispatcher **`ansible/playbooks/wire-logging.yml`** (`-e role= -e target=`, `ignore_unreachable`, resolves
  the target's inventory group from the module + the collector host from `instance/instance.yml`). Each role is
  idempotent, `--check`-safe, carries an access-chain header + a `backup` (rollback-reference) entrypoint, and
  `no_log`s any credential. `syslog_push` delegates to a NEW generic **`backend_netcommon_cli` `configure`**
  entrypoint (idempotent `cli_config`) — the `device_role` dispatch seam, no per-vendor branching.
- **Mgmt-bound syslog ingress** — `docker/services/vector.yaml` now publishes `${KONTROLL_MGMT_IP}:5514/tcp`
  (the one port; the Vector API stays loopback-internal), so a device configured by `logging_syslog_push` has a
  mgmt-VLAN-only landing spot for its RFC5424 syslog (C3/C12 parity with loki/prometheus).
- **Promote-path fix (`logsvc.py`):** `build_logging_plan`/`regenerate_logging` referenced
  `docker/vector/config.d/sources|transforms/generated/` — paths `gen-logging.py` never writes (it writes one
  fragment per `(method,key)` under `docker/vector/generated/` + the aggregate `_capability_sink`). A promote
  therefore committed phantom paths and never staged the real Vector config. Now single-sourced from the
  generator's `GEN_DIR` (incl. the regenerated sink), pinned by a drift test. (Missed because the unit tests
  no-op regen under `tmp_repo` and the capability path was never dogfooded.)
- **Scope (honest):** `syslog_push` is the end-to-end-provable path; `journald_remote`'s persistent-journald +
  `file_tail_ssh`'s read-key grant are built host-side, while their collector-side receiver / `exec` source are
  tracked follow-ups (documented in the role READMEs + `docs/logging-architecture.md`).
- **Tests:** `test_logging_wiring.py` (every named `wiring_role` is a real role w/ a backup entrypoint; the
  dispatcher + the backend `configure` exist) + a `logsvc` drift-pin (planned paths == the generator's output);
  `test_compose_logging.py` updated for the new mgmt-bound-ingress invariant.

### feat(capability): logging — capability-derived log ingestion, isomorphic to telemetry (2026-06-16)
The dual-purpose payoff: a `logs:` module block that ships a device/service's logs into Loki via Vector,
added through the SAME standalone capability dialog as telemetry/backup (PROPOSE→PROMOTE→ENACT) with **zero
spine/shell/route edit** — the third proof the secondary-capability seam generalizes. Built on the blackboard
design (`docs/reviews/2026-06-16-logging-vector/`) with V1's must-fixes applied (retention rides as a method
param, not a forbidden GUI stage; one canonical label set; the generated tree validates).
- **`capabilities/logging.yml`** (instance #3) + **`vectors/logging.yml`** (the weak detection dimension —
  only cliconf/netconf network gear auto-suggests `syslog_push`; journald/file/rest are operator-DECLARED).
- **`logging/<method>.yml` registry** (4 methods: `syslog_push`/`journald_remote`/`rest_pull`/`file_tail_ssh`)
  + **`catalog.load_logging()`** + **`paths.LOGGING_DIR`**.
- **`classify.suggest_logging`/`declared_logs_methods`/`_LOGGING_HINT`** (PURE; a drift-pin test holds the hint
  == the descriptor copy) + **`scripts/kontroll/service/logsvc.py`** (the `logs:` write + the Vector-reload
  enact builder, instance-local so `enact_kind: vector_reload` is data, not a spine edit).
- **`scripts/gen-logging.py`** — fans enabled modules × `logs:` × inventory → Vector source/transform drop-ins
  + one aggregate Loki sink under `docker/vector/generated/` (a disjoint config-dir, explicit ids); fail-closed
  on an unknown method, a param outside the allow-list, or a non-canonical/secret label. A `gen-logging --check`
  validate gate keeps it honest.
- **Shared `service/_blockwrite.py`** — the comment-preserving block writer + config-injection guard, lifted
  out of `observe.py` so telemetry + logging can't re-diverge (test-pinned).
- **Worked example:** `cisco_ios` declares `logs: [{method: syslog_push}]` (the suggested path); the generated
  Vector fragments are committed.
- **Tests** (all docstringed): `test_logging_{suggest,methods}.py`, `test_logsvc.py`, `test_gen_logging.py`,
  `test_blockwrite.py` — registry/labels/suggest purity/no-if-cap/generator fail-closed/idempotent-write.
  **Follow-up (now built — see the wiring entry above):** the device-side wiring roles (`roles/logging_*`) +
  `wire-logging.yml` (the push-method enact actuation) — the propose→promote→generate→enact flow is complete.

### feat(logging): internal Loki+Vector stack + Vector collector decision LOCKED (2026-06-16)
The logging Layer 1+2 (collector → store) lands as additive compose drop-ins; the collector fork (Vector vs
Alloy) is **decided = Vector** (vector.dev) — its source breadth (`docker_logs`/`journald`/`file`/`syslog`/
`http_*`/`exec`) makes it the load-bearing pick for the upcoming dual-purpose logging capability, and it ships
the internal logs too. Built on `feat/logging-vector`; deploy is opt-in (`stack_services` += `loki`,`vector`).
- **Loki** (`docker/services/loki.yaml` + `docker/loki/loki-config.yml`): single-binary, filesystem TSDB v13,
  compactor-driven **bounded retention** (30d global via `LOKI_RETENTION_PERIOD`; debug 7d, api/gui-audit 90d).
  The store is a **bind mount** at `/var/lib/kontroll/loki` (`0750`/uid-10001) — inside the C8 at-rest boundary
  (log bodies may carry a leaked secret). `:3100` binds `${KONTROLL_MGMT_IP}` only.
- **Vector** (`docker/services/vector.yaml` + `docker/vector/`): `--config-dir` drop-in tree (one file per
  source under `config.d/sources/`, a shaping `transforms/`, the single `sinks/loki.yaml` label choke-point);
  tails container logs + host journald + the GUI/API audit logs + the ansible run-log + the script run-logs.
  Runs root with **read-only** docker-socket/journald/file mounts; publishes **only a mgmt-bound syslog ingress**
  (`:5514`, added in the wiring follow-up above — the API stays loopback-internal).
- **Canonical, non-secret Loki label set** (the C12 hard rule): `source,host,service,level,run_id,device`.
- **Grafana Loki datasource** drop-in (`provisioning/datasources/loki.yml`, `uid: loki`) — the reserved seam.
- **Two Layer-0 durability fixes folded in:** the API audit log moves off ephemeral `/tmp` to persistent
  `/var/lib/kontroll/api/audit`; the ansible run-log gets a persistent `ANSIBLE_LOG_PATH` mount — both so they
  survive restarts AND Vector can tail them.
- **Security:** new **SECURITY.md C12** (logs are a secret surface — collector privilege, label hygiene,
  mgmt-only) + C8 extended (the Loki store + Vector buffers at rest). **Tests:** `test_compose_logging.py`
  (mgmt-bind, no-host-port, RO mounts, bind-mount store, bounded retention, label allow-list) + a
  `vector-config` validate gate. Doc: `docs/logging-architecture.md` Layer 1+2 + §3.5/§6 flipped to DECIDED.

### fix(install)! — make a fresh-node install actually complete: 14 blind-dogfood blockers (genericization Phase 5) (2026-06-15)
A LIVE blind install on a clean Debian 13 VM from the release bundle (following ONLY the docs) surfaced 14
fresh-node blockers — each a real thing that stopped a brand-new operator. All fixed; the full core+GUI stack now
stands up end-to-end (homepage/semaphore/grafana/prometheus/onboard-gui all serving, ports mgmt-bound; Semaphore
wired with 4 operational + 5 backup templates). Recurring theme: **device-cred / post-deploy artifacts (the
Proxmox token, the scoped Semaphore key, the GitHub deploy key, SNMP creds) must be OPTIONAL on a fresh node, not
assumed present.**
- **Stage 0** (`scripts/install-prereqs.sh`): install **sops + age** (were missing — `kontroll-init --fresh` needs
  them before bootstrap) and align the **Docker apt repo** to deploy-stack's keyring (`docker.asc` + a deb822
  `.sources`) so the two no longer collide on `Signed-By`.
- **`kontroll-init --fresh`** (`scripts/kontroll-init.py` + `secret-forms/semaphore.yml`): also mint the
  **`semaphore`** bootstrap domain (deploy-stack's `.env` needs it before the GUI exists), honour a field's
  `default:` (so the admin user/email aren't randomised), and SKIP a fully-provisioned domain on a re-run
  (idempotent — a regen would rotate `semaphore_db_password` and break the live DB).
- **Secret writer** (`scripts/kontroll/gitio.py`): create the overlay's `secrets/` dir before the first domain
  write (the scaffold skips it). **Key seed** (`keygen.py`): strip the template's stale `# REPLACE` so the later
  additive break-glass key-add doesn't fail `no_recipients` on a fresh overlay.
- **`bootstrap.yml`**: create the gitignored `ansible/collections/` dir (absent in a bundle).
  **`local-canonical.yml`**: commit the scaffolded `instance/` overlay so Semaphore's canonical has the config.
- **`deploy-stack.yml`** + **`docker/services/onboard-gui.yaml`**: tolerate a MISSING device-cred domain
  (proxmox/snmp render empty, not a hard fail); guard the snmp render; **generate the onboard-gui self-signed
  cert** (the API block had it, the GUI block created only the dir → the HTTPS GUI crash-looped); and render the
  GUI's TLS env per `frontend.tls_mode` so **byo_proxy serves raw HTTP** (the cert paths were hardcoded, so
  byo_proxy would have crashed the GUI — F16, found while completing the GUI dogfood).
- **`configure-semaphore.py`**: the **github-deploy** key and the scoped **SOPS_AGE_KEY** are now OPTIONAL — a
  fresh node has neither yet (the canonical repo uses the `none` key; the scoped key is minted later via the GUI).
- `configure-semaphore.py`: wait for Semaphore's API to come up before configuring (the container is ready before
  its HTTP server is) — a fresh deploy-then-configure flow otherwise raced it with a `ConnectionResetError`.
- Tests: F3/F4/F8/F9/F15 pinned as units (`test_secrets_service`, `test_keygen_service`, `test_kontroll_init`,
  `test_configure_semaphore`); the ansible/shell fixes are PROVEN by a clean single-pass install on a
  freshly-wiped VM (zero manual patching — every service reaches 200).

### harden(repo) — the release bundle ships the tool only: strip the entire `instance/` overlay + `docs/reviews/` (genericization Phase 5, clean-bundle) (2026-06-15)
- `scripts/make-bundle.sh` previously stripped only `instance/secrets/*.sops.yml` + onboarded hosts — so a
  distributed bundle still carried this instance's `.sops.yaml` recipients, inventory IPs, fleet, `instance.yml`,
  and Homepage dashboards (and the internal `docs/reviews/**` snapshots, which leak IPs/domains). It now strips the
  **entire `instance/` overlay** + `docs/reviews/`, shipping only the public `instance.example/` stub. A fresh
  operator scaffolds their OWN overlay with `kontroll-init --fresh` — never inheriting the author's recipients
  (the blind-Joe footgun).
- `scripts/install.sh` no longer hand-creates a half-built `instance/fleet.yml` (a vestigial line that contradicted
  its own comment and, with the overlay now stripped from the bundle, would have fired): `kontroll-init --fresh` is
  the single, tested scaffolder. New `tests/integration/test_make_bundle.py` builds the real bundle and pins the
  strip (no `instance/`, no `docs/reviews/`; the `instance.example/` stub + product code survive). Docs synced:
  `scripts/README.md`, `SECURITY.md` C8, `docs/local-source-of-truth.md`.

### docs(repo) — sync every doc to the instance overlay + the generic install flow (genericization Phase 4) (2026-06-15)

- After Phase 1–3 moved instance config into `instance/`, parameterized the mgmt IP/domain, and added
  `kontroll-init --fresh`, the docs still pointed at the pre-overlay paths and the old flow. A 6-agent read-only
  audit mapped every drift; this commit actuates the fixes (79 files). No behaviour change — docstrings, comments,
  user-facing messages, and prose only (the `paths.resolve("config/fleet.yml")` resolver idiom is preserved — that
  legacy string is the `_OVERLAY_MAP` key, not a path).
- **Executable-path drift** (a reader who runs it hits the wrong place): every `ansible/secrets`→`instance/secrets`,
  `config/fleet.yml`→`instance/fleet.yml`, `config/instance.yml`→`instance/instance.yml`, `ansible/inventory`→
  `instance/inventory`; the `sops updatekeys`/`sops <domain>` recipes now carry `--config instance/.sops.yaml` + the
  overlay glob; broken `See also` links repointed; `galaxy.py` dry-run hints + `gen-*` provenance headers (regenerated).
- **Install-flow rewrite** (the blind-Joe audit found a from-scratch install was NOT completable): SETUP.md /
  install-from-scratch.md / install.sh / install-prereqs.sh / bootstrap-control-vm.md now lead with
  `kontroll-init --fresh`, name the three edit points (fleet/inventory/instance.yml), fix the
  configure-semaphore-before-deploy ordering, add the GUI URLs/ports/logins, the onboard-gui cert/key prereq, and
  steer the C10 token to the GUI Secrets form (not the `sops --set`/`openssl` argv-leak C11 forbids).
- **Prose IP/domain de-leak** (live docs only; the internal review snapshots were frozen for the Phase-5 scrub): the
  literal mgmt IP → `<mgmt-ip>`/`${KONTROLL_MGMT_IP}` and the literal domain → `<your-domain>`/`${KONTROLL_DOMAIN}` across README, SECURITY,
  api/gui/dashboards READMEs, and docs/. CLAUDE.md governance paths + the SECURITY.md control→file refs corrected.
- Windows-verified: full suite (340) + gen `--check` fresh + yamllint + test-docs all green; resolver idiom + line
  endings (LF via .gitattributes) confirmed intact.

### feat(repo) — parameterize the mgmt IP + domain; relocate Homepage config to the overlay (genericization Phase 3, PR-2) (2026-06-15)
- The genericization payoff proper: the shipped compose layer no longer hardcodes this instance's mgmt IP or domain.
  **mgmt IP →** `${KONTROLL_MGMT_IP:?set in .env}` on all 6 privileged published ports (api 8444, onboard-gui 8443,
  prometheus 9090, grafana 3002, caddy 443/443udp/80) — **fail-closed** `:?` so an unset value HARD-fails `compose
  config` instead of silently binding 0.0.0.0 (the NPM-fronted homepage 3000 / Semaphore 3001 stay bare, as before).
  **domain →** `${KONTROLL_DOMAIN}` in grafana `GF_SERVER_ROOT_URL`, homepage `HOMEPAGE_ALLOWED_HOSTS`, and the
  Semaphore admin-email default; the `docker/.env.example` domain **leak** (a real-domain admin e-mail default) is fixed.
- **Both `.env` render paths move together:** `docker/.env.example` gains `KONTROLL_MGMT_IP=127.0.0.1` (safe loopback
  example) + `KONTROLL_DOMAIN=example.com`, and `deploy-stack.yml` renders both into the live `docker/.env` sourced
  from `instance/instance.yml` (`mgmt_ip`/`domain`, added in PR-1). Updating only one would leave the live deploy
  binding 0.0.0.0 while `validate` stayed green — the trap the read-only sweep flagged.
- **ii-d:** `git mv dashboards/homepage/* → instance/dashboards/homepage/` (your portal tiles are instance state,
  not tool code; Homepage can't interpolate `${KONTROLL_DOMAIN}`). Repointed the homepage compose bind-mount,
  its header comment, and `config/state-manifest.yml`. The tool ships the generic stub at
  `instance.example/dashboards/homepage/` (PR-1); a fresh node's `--fresh` scaffolds the overlay copy.
- **Guards (first-class, same commit):** new `tests/check-mgmt-port-binding.py` — every published port binds
  `${KONTROLL_MGMT_IP}` or is a documented NPM-fronted exception — wired into `validate.{sh,ps1}` (the C3 covering
  check, SECURITY.md updated); and `tests/unit/test_mgmt_parameterization.py` pins no-literal-survives + both
  render paths. Windows-verified (guardrail + 340-test suite + yamllint + test-docs green); `compose config` +
  ansible-lint in CI. **LIVE-TOUCHING** (re-renders `.env` + recreates containers) → scratch-verified on a VM
  worktree before the live apply.

### feat(repo) — ship the `instance.example/` overlay skeleton + `kontroll-init --fresh` (genericization Phase 3, PR-1) (2026-06-15)
- The genericization payoff begins: a fresh operator can now stand up their OWN private `instance/` overlay instead
  of inheriting this instance's config. Ships `instance.example/` — a placeholder mirror of `instance/`
  (`.sops.yaml`, `instance.yml`, `fleet.yml`, `inventory/hosts.yml`, `dashboards/homepage/*`, `secrets/README`) with
  RFC 5737/2606 placeholders (`192.0.2.x`, `example.com`) and a single obviously-fake age recipient.
- **`kontroll-init --fresh`** scaffolds `instance/` from that skeleton (never overwriting an existing file, never
  copying the `secrets/` subtree), mints the control key, and writes `instance/.sops.yaml` naming **ONLY** that key —
  `keygen.seed_fresh_sops()` REPLACES the placeholder rather than the additive `add_recipient`, so a brand-new node
  never inherits another instance's recipients (the inherit-recipients footgun the blind-Joe audit found). `--mgmt-ip`
  / `--domain` / `--tls-mode` flags fill `instance.yml`; a `_fresh_guard` refuses on an already-configured tree
  (real recipient, no placeholder) BEFORE any write.
- **Security test (first-class, same commit):** `test_fresh_writes_sops_with_only_new_recipient` pins that every
  domain names exactly the minted key and the private key is never printed; siblings cover the skeleton-without-
  secrets scaffold + the configured-tree refusal.
- Adds `mgmt_ip`/`domain` to `instance/instance.yml` (inert until PR-2's deploy-stack renders `${KONTROLL_MGMT_IP}`/
  `${KONTROLL_DOMAIN}`). `.ansible-lint` excludes `instance.example/` (shipped template data, not ansible content).
  `instance/` stays tracked (the Phase-5 public split owns the gitignore). No live impact — additive; full suite +
  yamllint + pyflakes + test-docs green on Windows, ansible-lint/gitleaks in CI.

### refactor(repo)! — relocate instance config into the `instance/` overlay (genericization Phase 2 step ii-c) (2026-06-15)
- The move: `git mv` the five instance path-sets into one `instance/` overlay — `ansible/secrets→instance/secrets`,
  `ansible/inventory→instance/inventory`, `config/{fleet,instance}.yml→instance/`, `.sops.yaml→instance/.sops.yaml`
  (inventory + secrets kept SIBLINGS + `.sops.yaml` at the overlay root so sops walk-up discovery + the group_vars
  `inventory_dir/../secrets` lookup still resolve). The Python layer follows transparently via the resolver
  (steps i/ii-a); this commit repoints the ~20 NON-resolver consumers a read-only completeness sweep found.
- **Atomic `.sops.yaml` recipiency rewrite (one commit):** all 7 per-domain `path_regex` + keygen's parse-regex +
  `SOPS_UPDATEKEYS` + the recipiency test moved `ansible/secrets/`→`instance/secrets/` together — a split would
  SILENTLY drop the scoped Semaphore key from network/proxmox via the catch-all rule.
- **Ansible/shell repoints:** `ansible.cfg` inventory → `../instance/inventory`; `deploy-stack` (4 SOPS lookups +
  `instance.yml`); `bootstrap` (fleet / `.sops.yaml` break-glass check / secrets stat); `configure-semaphore`
  Semaphore inventory object → `instance/inventory`; `backup.sh` / `install.sh` (seeds `instance/fleet.yml`) /
  `run-smoke-gate.sh` / `make-bundle.sh`; `tests/e2e/inventory.yml`.
- **CI/lint kept green across the move:** `.gitleaks.toml` / `.yamllint` / `.ansible-lint` gained `instance/secrets/`
  entries (KEEPING the `ansible/secrets/` lines so full-history gitleaks stays clean); `validate.sh` + `.ps1`
  sops-encrypted gate now walks `instance/secrets` (else it passed VACUOUSLY); `state-manifest.yml` state-paths +
  notes repointed (the DR / no-clobber boundary — stale ⇒ data-loss on restore).
- `instance/` stays TRACKED in this private repo (the deploy canonical; encrypted ciphertext must be committed, as
  `ansible/secrets` was — gitignoring it belongs to the future public-tool split). Verified on Windows (pyflakes,
  generator `--check` byte-identical, yamllint, `bash -n`, full suite green); ansible-lint + the live SOPS recipient
  round-trip verify in CI + on a VM scratch clone before merge to main.

### refactor(repo) — instance-overlay resolver: WRITE paths + constants + test loaders (genericization Phase 2 step ii-a) (2026-06-15)
- A read-only completeness sweep (6 agents over every non-Python consumer of the 5 instance paths) surfaced a blind
  spot in step (i): the resolver covered READERS but not WRITERS — three code-half sites would FAIL SILENT once
  secrets move (encrypt into the overlay, then git-add the LEGACY path → the credential is never staged/committed).
  Closed here, still **zero behavior change** today (no `instance/` dir ⇒ every resolve falls through to legacy).
- **New `paths.overlay_target(rel)`** — DIR-active (vs `resolve()`'s file-existence): the WRITE location for a file
  that may not exist yet, so a new drop-in host / secret domain lands in the ACTIVE overlay AND its git-add path
  equals the write target (the C10 staging invariant). The legacy `paths.SECRETS_DIR`/`SOPS_CONFIG` constants
  (import-bound to the legacy tree — the root-cause trap) were removed; secrets/`.sops.yaml` now resolve via the seam.
- Repointed writers: `onboard.py` (`inv_path` + `paths_changed`), `secrets.py` (`_existing_keys` read + the staged
  `path`), `gitio.sops_write_domain` (write path + `--filename-override`, both now `overlay_target`). Repointed the
  gen-TESTS that bypassed the resolver via raw `_load("config/fleet.yml")` (would FileNotFound / collection-error
  post-move) + the conftest/keygen fixture SOURCE copies — so the suite is move-ready.
- Tests: `test_paths_overlay.py` gains `overlay_target` coverage incl. the write==git-add agreement pin. Verified
  on Windows: pyflakes clean, generator `--check` byte-identical, full suite green.

### refactor(repo) — instance-overlay resolver seam: Python readers prefer a private `instance/` overlay (genericization Phase 2 step i) (2026-06-15)
- Groundwork for releasing kontroll as a PUBLIC tool while preserving this deployment's config: the repo is today
  a single live instance (real `.sops.yaml` recipients, committed `ansible/secrets/*`, the inventory, the mgmt IP),
  so a fresh installer is blocked. The arc separates all instance config into a private, gitignored `instance/`
  overlay (Decision A); the tool ships only generic stubs. This first step introduces the resolver and repoints
  every PYTHON reader — **zero behavior change** (no `instance/` dir exists yet, so every resolve falls through to
  today's legacy path; proven below). Plan + cut-line manifest live in `local/` (gitignored).
- **New `kontroll.paths.resolve()` / `overlay_rel()`** — overlay-preferred with PER-FILE legacy fallback (a
  half-populated overlay never half-breaks a running instance). Mapped paths: `config/fleet.yml`,
  `config/instance.yml`, `.sops.yaml`, `ansible/inventory`, `ansible/secrets` (the last two keep their sibling
  layout inside the overlay so ansible's `inventory_dir/../secrets` lookup + the group_vars SOPS plugin still
  resolve). ROOT is read dynamically so the `tmp_repo` fixture's repoint still diverts mutators.
- **Repointed readers:** the 6 generators (`gen-observability`/`-prometheus-jobs`/`-exporters`/`-backup`/
  `-requirements`, `fetch-dashboards`), the `audit`/`classify`/`onboard`/`keygen` services, and `gitio` — including
  the sops `--filename-override`, now derived from the resolved path so it auto-tracks the file's location and
  stays matched to `.sops.yaml`'s `path_regex` (manifest §4, coupling 1).
- **Verified (Windows):** pyflakes clean; all four generator `--check` staleness guards byte-identical; the
  unit/integration suite green, incl. new `tests/unit/test_paths_overlay.py` pinning the zero-change invariant +
  per-file fallback + the inventory/secrets sibling mapping.
- Deferred to step (ii) (the live-verified file move): the ansible/shell readers (`ansible.cfg` inventory,
  `deploy-stack`/`backup.sh` `instance.yml` lookups), the `.sops.yaml` `path_regex` rewrite + keygen parse regex
  (the atomic coupling), and write-of-NEW-file target semantics.

### feat(observability) — enact "make it live" is now a GUI action for the Tier-1 steps (item F) (2026-06-15)
- After a promote, "make it live" (enact) was hand-off command strings. It now splits by what the command needs:
- **Tier-1 → one-click Semaphore tasks (no new privilege):** the universal **Prometheus config reload** becomes
  the `reload-observability` task — Prometheus now runs with `--web.enable-lifecycle` and the task POSTs `/-/reload`
  over the shared docker network (no `docker exec`, no host root, no restart); the **host-agent install** becomes
  the `install-node-exporter` task (a normal fleet play — tick Semaphore's Dry Run for the `--check` pass first).
  Both are drop-ins in `config/semaphore/templates/`.
- **Tier-2 → operator-run, GUI-shown (operator's explicit choice):** `deploy-stack` (bring up an exporter
  container / provision dashboards / rebuild the runner) stays an operator command because it needs host Docker
  control (the docker socket = host-root on the control node); the GUI surfaces it clearly. The backup-schedule
  registration (`gen-backup` + `configure-semaphore`) stays operator-run (needs the Semaphore admin creds).
- **Enact data model gains `kind`** (`semaphore` | `operator`): each step keeps `cmd` + `why` (CLI fallback) and
  adds `kind` (+ `task`/`args` for Semaphore steps). The GUI renders ▶ for a one-click Semaphore task and `$` for
  a control-node command. `observe.py`/`backup.py` enact builders updated; the host-agent enact also fixed to
  `-e target=<host>` (the playbook's var) instead of a `--limit <module-key>` that matched no host.
- Tests: `test_observe.py` pins the new `kind` contract + that the reload is a Semaphore task (no `kill -HUP`);
  `test_configure_semaphore.py` (registry) already asserts every template's playbook exists. SECURITY.md accepted
  risk for `--web.enable-lifecycle` (mgmt-only `/-/quit`).

### hardening(security) — adversarial-review fixes for the C10 propose/promote path (item C) (2026-06-15)
- A 3-lens adversarial review of item C confirmed the core invariant (a leaked API token can only PROPOSE;
  promote is Semaphore-admin-only, never token-reachable) and surfaced hardenings, now folded in:
- **All three network containers now gate the canonical mount** (`:${KONTROLL_CANONICAL_MODE:-ro}` on api +
  onboard-gui, matching semaphore) — previously api/onboard-gui mounted `/srv/kontroll.git` `:rw`
  **unconditionally**, so the "read-only deploy untouched" claim was overstated. The read-only deploy now mounts
  the canonical `:ro` on every container; `:rw` exists only when armed.
- **Fail-closed propose gate.** `gitio._push_target` now RAISES when a staging service reaches a push without a
  `run_id` (was a silent fallback to a direct `main` push), resolved BEFORE any mutation; and
  `api/settings.from_env` REFUSES to start a token-bearing API without `KONTROLL_STAGE_PUSHES` — so the
  propose-vs-`main` gate can't silently decouple (hand-edited `.env` / partial deploy) into a direct-`main` write.
- **promote.yml run_id assert pinned to the minted grammar** (`^([0-9a-f]{12}|<UTC>T<HMS>Z-<6hex>)$`) — rejects
  typo'd/guessed branch names at the survey boundary (defence-in-depth behind the literal `proposed/` prefix + FF
  gate). **promote_ref** now reports a visible note if the post-FF proposal cleanup fails (was a silently-ignored
  rc), so a half-completed promote isn't reported as fully done.
- **SECURITY.md C10 made honest:** when armed, a *container RCE* (not a token leak) can `update-ref`/rewrite/
  delete `main` directly — `update-ref` is not a push, so deny*/FF-only bound the promote *playbook*, not a
  compromised container. Accepted, bounded by the reconstructible-cache + age-key-dominance + mgmt-VLAN + the
  default `:ro` mounts. Tests: `test_privmut_staging.py` (fail-closed raise + cleanup-note + the from_env coupling).

### feat(semaphore) — promote is now a GUI "approve" task, not a manual sudo (item C) (2026-06-15)
- **The C10 promote moves into Semaphore.** A new **`promote-proposal`** Semaphore template (with a `run_id`
  **survey variable**) fast-forwards `main` into a staged `proposed/<run_id>` via `ansible/playbooks/promote.yml`
  — which asserts the run_id shape, checks the proposal exists, previews the commits being approved (job-log
  audit trail), then runs the ONE tested FF-only promote (`scripts/kontroll-promote.py` → `gitio.promote_ref`).
  The network service (API/GUI) can only PROPOSE; this promote is reachable **only by a Semaphore admin on the
  mgmt VLAN, never the API token** — so a leaked token can never self-promote. Resolves install-from-scratch §7
  step 4 [GAP] (promote was a manual `sudo kontroll-promote.py`).
- **Operational templates are now a drop-in registry.** `config/semaphore/templates/<name>.yml` (ping-fleet,
  promote-proposal) — `configure-semaphore.py` loops it and `upsert`s one template per file (survey_vars
  threaded), so **adding an operational task is a new file, never a hub edit** (the "add an X" doctrine). The
  former inline ping-fleet migrated in. The scheduled (backup) templates stay generated in
  `schedules.generated.yml`; the two registries split operational-vs-scheduled.
- **Gated rw canonical mount for Semaphore.** deploy-stack renders `KONTROLL_CANONICAL_MODE=rw` under
  `api_privileged` (default `:ro`, read-only deploy untouched); `semaphore.yaml` mounts the canonical
  `${KONTROLL_CANONICAL_MODE:-ro}` and forces **`user: "1001:1001"`**. The latter was found live: the stock
  image's `USER 1001` resolves to **gid 0** (not in group 1001), so a job could not write the group-1001
  canonical even when mounted `:rw`; forcing gid 1001 (like api.yaml) makes the job a group member. The promote
  then runs as **gid 1001** (no host root) thanks to `core.sharedRepository=group` (added to
  `local-canonical.yml`) + the B group-write grant, which make newly-pushed refs/objects group-writable so a
  gid-1001 promoter can advance `main` and delete the proposal. Include `semaphore` in `stack_services` when
  arming so it is recreated with the rw mount + the gid.
- **Trust-boundary delta (operator-accepted):** arming the GUI promote gives the Semaphore container standing
  `:rw` on the canonical — a Semaphore compromise could also `update-ref`/delete canonical refs (bypassing the
  deny* hooks). Bounded: the canonical is a reconstructible cache (workstation + GitHub are truth), and
  Semaphore's dominant residual (fleet root via the scoped age key) already dominates. SECURITY.md C10 (new
  bullet + residual + accepted-risk row). Tests: `test_configure_semaphore.py` (registry loads ping+promote,
  promote declares the required run_id survey var, every playbook path exists, payload threads survey_vars only
  when present); the FF-only contract stays pinned by `test_privmut_staging.py`.

### fix(ansible) — recursive canonical write-grant is now an idempotent task (item B) (2026-06-15)
- `local-canonical.yml` gains the **recursive gid-1001 write-grant on existing canonical objects** — the
  one-time manual `sudo chgrp -R 1001 + chmod -R g+w + find -type d setgid` step on `/srv/kontroll.git`
  (needed when a canonical predates the C10 grant; a fresh one is covered by the top-dir setgid). It chgrps
  recursively via the file module (accurate change reporting) and applies group-write + dir-setgid only when a
  **detect** step finds an object lacking it — so a second run reports `0 changed` (idempotent). Closes
  install-from-scratch §3 [GAP]; SECURITY.md C10 (the write-grant mechanism).

### feat(setup) — from-scratch bootstrap automation: install-prereqs.sh + kontroll-init (item E) (2026-06-15)
- **`scripts/install-prereqs.sh`** — the ONE idempotent system-dependency installer for a fresh Debian/Ubuntu
  control node: ansible, git, **Docker CE + the compose plugin** (the previously-unautomated gap — the whole
  stack runs in compose, yet no step installed Docker), python3 + pip + venv, acl, openssl, jq. `./bootstrap.sh`
  is now a thin shim that `exec`s it (the documented command keeps working but now installs everything,
  including Docker). Resolves install-from-scratch §0 [GAP].
- **`scripts/kontroll-init.py`** — the keygen split's CLI half: the bootstrap a *running* GUI can't do for
  itself. It (1) ensures the **control age key** (`age-keygen -o ~/.config/sops/age/keys.txt`, 0600), (2) adds
  its public half to `.sops.yaml` (base + ops) via the shared **parse-verified** `keygen.add_recipient`
  (additive + idempotent — closes the manual SETUP §5.1 step), and (3) mints the `dashboards` **bootstrap
  secrets** if absent — the onboard-GUI password (the genuine chicken-and-egg: the GUI can't set the password
  it needs to start), the Grafana admin password, and the C10 API token — encrypted into SOPS, **showing the
  login passwords once**. Idempotent; reuses the tested keygen + secrets services. Unlike the GUI keygen
  (never-persist), it writes the control private key to its canonical 0600 path — the legitimate local-operator
  exception (SECURITY.md C11).
- **`dashboards` domain gains `gui_admin_password`**, rendered to `GUI_PASSWORD`/`GUI_USER` in deploy-stack's
  `.env` (with a safe default so an instance that hasn't minted it yet still renders) — closing the
  GUI_PASSWORD wiring so onboard-gui is SOPS-sourced, not a hand-set env var. `keygen.add_recipient` was
  extracted from `apply_keygen_plan` so the GUI keygen and kontroll-init share one parse-verified writer.
- Tests: `test_kontroll_init.py` (which login secrets it generates — never clobbering an already-set field; the
  age-public parse; idempotent main never prints the private key). The dashboards-domain tests across layers
  updated for the new required field. Docs: SETUP §1/§5, install-from-scratch §0/§2 + summary table.

### feat(keygen) — guided age key-generation: break-glass / Semaphore / control-rotation (D, part 4) (2026-06-15)
- The second half of the secret/key wizard, per the operator-agreed **split**: a GUI surface that mints age
  keys a *running* GUI can mint (the first control key + `GUI_PASSWORD` stay a CLI `kontroll-init` bootstrap,
  item E — a GUI can't mint the key it needs to run). A header **"Keys"** button opens a role picker (the new
  `key-roles/<role>.yml` registry — break-glass / scoped-Semaphore / control-rotation), and choosing a role
  opens **one shared dialog** (role is data, not a branch — the key analogue of the secret dialog). Generate
  mints a keypair on the box and **shows the private key exactly once** (never stored, logged, audited, or
  committed — the operator saves it: offline for break-glass, at the key path otherwise); only the **public**
  recipient is added to `/.sops.yaml` and **staged** `proposed/<run_id>` (C10). The audit + commit carry the
  public key only.
- The `.sops.yaml` edit is **additive, idempotent, and parse-verified**: anchor-preserving text insertion into
  the named recipient group(s), guarded by a gate that refuses to write unless the edit added exactly the new
  recipient and dropped none (the crown-jewel guard against unrecoverable / over-exposed secrets). A scoped
  Semaphore key lands in `ops_recipients` only (network+proxmox), never the service domains. Minting a keypair +
  adding a public recipient need **no** age key (public-key ops) — consistent with C10's no-key posture; the
  decrypt-needing re-wrap (`sops updatekeys`) is returned as a **deferred** operator step, **never auto-run**.
- `service/keygen.py` (build/apply + the pure `.sops.yaml` editor + the parse-verify gate) + `gitio.age_keygen`
  (a silent shell-out — prints nothing, since stdout holds the private key) + `catalog.load_key_roles()` + the
  GUI Flask routes `/api/keygen[/<role>]`. **SECURITY.md C11** now covers both this and the secret-entry surface
  (no-leak by construction). Tests: `test_keygen_service.py` (private key only in the result; additive +
  idempotent + anchor-scoped edit; the verify gate rejects a dropped/smuggled recipient and writes nothing;
  `age_keygen` prints nothing), `test_gui_api.py` keygen cases (show-once response; audit/commit carry only the
  public key; staged not `main`), `test_keygen_flow.py` (e2e render). `key-roles/README.md` +
  `testid_reference.md` (`keygen-*`) + install-from-scratch §2 resolved. **GUI-only** this cut (API parity is
  an easy add).

### feat(gui) — secret-onboarding dialog: the guided GUI form for service/infra secrets (D, part 3) (2026-06-15)
- The operator-facing half of the headline ask: a header **"Secrets"** button opens a domain picker (the
  registered `secret-forms/`), and choosing a domain opens **one shared dialog** parametrized by the
  descriptor (domain is data, not a branch — the secret analogue of the capability dialog). Fields render by
  type (password/token masked, text plain); a `generate:` field offers a "gen" button that simply blanks the
  input so the **server** mints the value on save (it never round-trips to the browser). Save encrypts the
  values into SOPS in-process (the GUI holds the age key) and **stages** `proposed/<run_id>` (C10); the result
  pane shows the staged ref + the field NAMES set — never a value. Flask routes `/api/secrets`,
  `/api/secrets/<domain>/fields`, `/api/secrets/<domain>` mirror `api/routes/secrets.py`; the action + field
  names are audited, the values never are.
- Tests: `test_gui_api.py` adds the GUI secret routes (list, 404, the staged no-leak apply — value absent from
  both response and audit, required-missing → 422); `test_secrets_flow.py` (e2e) proves the picker → dialog →
  field render path + that the new dialog JS loads cleanly. `tests/testid_reference.md` records the
  `secrets-open` / `secret-*` testids. On-box age key-gen (the split: GUI for break-glass/semaphore/rotation,
  a CLI `kontroll-init` for the first control key) + the SECURITY.md control land next.

### feat(api) — secret-onboarding API route /secrets/{domain} (D, part 2 — the typed surface) (2026-06-15)
- The privileged HTTP surface over the secret-onboarding service: `GET /secrets/{domain}/fields` returns a
  domain's form fields + which are already set (NAMES only, never a value); `POST /secrets/{domain}` proposes
  (dry-run → the resolved VIEW, no values) or applies (encrypt the values into SOPS via the stdin whole-file
  seam, commit + **stage** `proposed/<run_id>`, C10). Token-gated + audited (the action + field NAMES, never a
  value); registered in `api/main.py`; `/secrets` added to the rate-limiter's privileged set (+ the tripwire's
  expected route set). A missing required field → clean 422; an absent age key (can't encrypt) → clean 500,
  never a clobbered domain. Tests (`test_api_secrets.py`) pin the no-leak contract over HTTP (a provided
  password + a generated token are absent from both the response and the audit), dry-run-by-default, and the
  error mapping. The GUI dialog + on-box age key-gen land next.

### feat(secrets) — secret-onboarding service foundation: a guided SOPS-domain entry seam (2026-06-15)
- First half of the secret/key onboarding wizard (the headline "automate-all / GUI-onboard the security
  boundary" goal): a generalized secret-onboarding seam mirroring the capability seam. `secret-forms/<domain>.yml`
  (a NEW drop-in registry — deliberately named `secret-forms/`, NOT `secrets/`, so it can never be mistaken for
  a place to drop a real value) declares each SOPS domain's form FIELDS (key/label/type/required/optional
  `generate`); `catalog.load_secret_forms()` is the loader. `service/secrets.py` builds a PURE plan (field NAMES
  + source: provided/generated/already_set/skipped — **never** values) and applies it by decrypting → merging →
  re-encrypting the whole domain via two new gitio seams (`sops_decrypt_domain` / `sops_write_domain`) that pass
  the plaintext over sops' **STDIN** — never argv (no `ps` leak) or a temp file. Idempotent (identical plaintext
  ⇒ no re-encrypt ⇒ no canonical churn). Descriptors shipped for dashboards/semaphore/snmp_observability/acme;
  network/proxmox/compute stay device-onboarded (per-host). The GUI/API surface + on-box age key-gen land next.
- Tests (`test_secrets_service.py`): the crown-jewel no-leak property (a provided value + a generated token
  appear in neither the client `view` nor the apply result), missing-required → clean error, already-set kept,
  whole-domain merge via the mocked sops seam, idempotence, decrypt-failure handling, generated-secret shape.

### feat(gui) — onboard runs in-process + STAGES, closing the last network-surface uniform-staging gap (2026-06-15)
- The GUI `/api/onboard` route no longer shells to the `galaxy.py` CLI (whose push goes straight to `main`); it
  now calls the service layer in-process — `build_onboard_plan` + `apply_onboard_plan` +
  `gitio.commit_and_push(run_id=…)` — exactly like `api/routes/onboard.py`. So a GUI onboard STAGES
  `proposed/<run_id>` when the deploy armed `KONTROLL_STAGE_PUSHES` (C10), never `main`: every network-surface
  canonical write (onboard **and** capability) now proposes uniformly. Bonus: credentials no longer pass through
  subprocess argv — they stay in-process (in-memory `creds_to_set`), encrypted to SOPS by apply. `run_galaxy` /
  `subprocess` retired from gui/app.py.
- Bootstrap (collection install + live-verify) is now DEFERRED to a post-promote Semaphore enact, not run inline:
  the network surface runs no Ansible, and bootstrapping a host that lives only in an un-promoted proposal is
  premature. The form's `bootstrap` flag becomes a "do this after promote" hint; the vestigial `commit` checkbox
  (apply now always commits + stages) was removed (the `field-commit` testid retired).
- Tests: the GUI onboard tests now mock the service seams (`gitio._run` / `probe` / `catalog` + `tmp_repo`)
  mirroring `test_api_onboard`, incl. a staging contract test (`KONTROLL_STAGE_PUSHES` ⇒ `proposed/*`, never
  main) and the creds-never-logged property; the e2e dry-run renders the in-process plan; the e2e conftest drops
  the `run_galaxy` mock. Full offline suite green; pyflakes clean.

### feat(docker) — C10 staging extended to onboard-gui: the GUI capability-promote path goes live (2026-06-15)
- The capability dialog is served by **onboard-gui** (Flask), but only the **API** clone was provisioned for C10
  staging — so the dialog's promote button couldn't reach the canonical. Now the gated deploy provisions
  onboard-gui identically to the API: `docker/services/onboard-gui.yaml` runs `user: "1001:1001"` (gid to write
  the group-1001 canonical) and mounts `/srv/kontroll.git` (so the clone's `local` remote resolves in-container);
  `deploy-stack.yml` grows a `'onboard-gui' in stack_services` block (host dirs, RO canonical clone, and — gated
  on `api_privileged`, now tier-wide — chown-to-uid-1001 + the `local` remote). When `api_privileged=true` a
  capability-promote from the GUI stages `proposed/<run_id>` (never `main`), exactly like the API. Read-only stays
  the default: each clone is root-owned ⇒ uid-1001 can't write it. **Live-verified on the VM 2026-06-15:** the
  gated deploy provisioned the onboard-gui clone (uid:gid 1001:1001 + `local` remote), a `run_id` push from it
  staged `proposed/<run_id>` with `main` left at its prior commit, and the reject path (`update-ref -d`) dropped
  the proposal cleanly.
- **Honest trust-boundary note:** onboard-gui mounts the scoped **age key** (inline device-cred encryption —
  `sops_set` needs decrypt-to-add), so unlike the no-key API it is the **with-key** surface; the no-key property
  is an API property (pinned for `api.yaml` by `test_privmut_no_key.py`). The GUI's standing-decrypt is
  pre-existing (the device-onboard alpha always held the key); this change only adds bounded, rejectable
  canonical-write. Tracked in SECURITY.md C10 residual + docs/privileged-mutation-enablement.md §6.
- The **GUI onboard** route was the one network-surface write that did NOT stage — it shelled to the `galaxy.py`
  CLI, whose push gate goes straight to `main`. (The **API** onboard route already staged via
  `gitio.commit_and_push(run_id=…)`; the gap was the GUI's alone, not tier-wide as first noted here.) Closed by
  the feat(gui) entry above — the GUI onboard route now runs in-process and stages too.

### docs(setup) — fresh-Debian "Joe" install guide: every step automated-or-GUI-onboarded (2026-06-15)
- `docs/install-from-scratch.md` — the from-scratch path grounded in the real C10 live-enablement run: system
  deps (incl. the live-found `acl`), keys/secrets (the SECURITY-BOUNDARY steps → guided onboarding), the local
  canonical + C10 hardening, the runner image, Semaphore, deploy-stack, the gated privileged enablement, and the
  guided onboarding/capability flow. Each step tagged **[A] automated** or **[B] security-boundary → GUI
  onboarding**, with **[GAP]** markers for what's still manual (the recursive write-grant, the secret/key
  wizards, onboard-gui staging, `install-prereqs.sh`, promote-as-Semaphore-task) — the operator's "automate it
  all, or GUI-onboard if it crosses a security boundary" directive made into a concrete punch-list.

### feat(docker) — C10 live-enabled + verified on the VM; two artifact fixes the live run surfaced (2026-06-15)
- The C10 privileged-mutation enablement was **turned on and verified live**: the auth gate inverts (no-token
  401 / valid 200), the API mounts **no age key**, a privileged write **stages** `proposed/<run_id>` (never
  `main`), `kontroll-promote.py` fast-forwards `main`, and `receive.denyDeletes`/`denyNonFastForwards` are
  enforced. Token rotated post-test; canonical left clean.
- Two fixes the live run surfaced (both now in the committed artifacts): `docker/services/api.yaml` runs the API
  as **`user: "1001:1001"`** (the gid is needed to write the group-1001 canonical) and **mounts
  `/srv/kontroll.git`** (so the `file://` `local` remote resolves inside the container — without it the staging
  push fails). `deploy-stack.yml` no longer uses `become_user` for the clone-remote (it needs the `acl` package
  and trips the unprivileged-become ACL path) — it runs as root with a `safe.directory` so the clone task is
  idempotent across re-deploys. `kontroll-promote.py` documents the **`sudo`** requirement (root/gid-1001 is
  needed to delete the container-created proposal ref after the FF). docs/privileged-mutation-enablement.md §5
  records the verified from-scratch recipe; SECURITY.md C10 → live-verified.


### docs(security) — C9→C10 privileged-mutation enablement: posture decided (propose-then-promote, no key) (2026-06-15)
- The decision record for turning on live actuation from the deployed API/GUI: a new
  `docs/privileged-mutation-enablement.md` + **SECURITY.md C10**. Posture **chosen** (1b threat-model rec #2):
  the always-on **network service** may only **PROPOSE** a canonical change (it pushes a `proposed/<run_id>`
  staging ref), and a trusted step (operator CLI / a Semaphore approval task) **PROMOTES** it to `main` —
  fast-forward only. **No decrypting age key** is mounted (capability writes carry no creds; onboard's
  cred-encryption defers to the promote step out-of-band — age can't encrypt-without-decrypt). The key reasoning
  correction: setting the token lights up **every** privileged route incl. high-blast onboard-apply, so the
  whole write surface needs the gate — not just the low-blast capability promote. Documents the three
  independent layers (provenance / enactment / scheduled autonomy) so it's clear propose-then-promote gates
  only the first and does **not** preclude scheduled jobs. Flag-gated (`api_privileged`, default off);
  unconditional `receive.denyNonFastForwards`/`denyDeletes` hardening. The mechanics build + live-enable steps
  follow; nothing actuated yet.

### feat(api) — C10 propose-then-promote staging mechanism (the network-service write half) (2026-06-15)
- `gitio.commit_and_push` gains `run_id`: when the caller is a staging network service
  (`KONTROLL_STAGE_PUSHES=1`), it pushes a per-request `proposed/<run_id>` STAGING ref instead of `main` — so
  the always-on API/GUI can only PROPOSE a canonical change. `gitio.promote_ref` is the trusted PROMOTE half
  (fast-forward `main` ← `proposed/<run_id>`, **FF-only**, then delete the proposal); `scripts/kontroll-promote.py`
  is the operator CLI (a Semaphore approval task is the in-GUI follow-on). The API onboard/capability/
  capture-exception routes + the GUI capability route thread their `run_id` and surface the staged ref in the
  response ("promote it: `kontroll promote <run_id>`"). Unset env (operator CLI / non-staging deploy) ⇒ direct
  to `main`, unchanged. Pinned by `tests/unit/test_privmut_staging.py` (stages-not-main, FF-only promote,
  refuse-non-FF). Inert until the env is set by the gated deploy — the deploy-artifact enablement + the
  live-enable steps follow (SECURITY.md C10).

### feat(docker) — C10 deploy enablement: flag-gated, no key, default off (2026-06-15)
- The deploy artifacts that flip the API/GUI to privileged-capable, all gated by `deploy-stack`'s
  `api_privileged` (default **off** — the read-only deploy is provably untouched): `docker/services/api.yaml`
  (soft-default `KONTROLL_API_TOKEN`/`KONTROLL_STAGE_PUSHES` env; the content mount's writability is now
  controlled by the clone's OWNERSHIP — root-owned ⇒ uid-1001 can't write it — not the mount flag; **no age
  key**), `docker/services/onboard-gui.yaml` (the stage env), `deploy-stack.yml` (the `api_privileged` flag +
  gated token render + gated chown-the-clone-to-uid-1001 + `local` remote), `local-canonical.yml` (the uid-1001
  write grant via shared-group+setgid + the **unconditional** `receive.denyNonFastForwards`/`denyDeletes`
  hardening that protects the operator's own pushes too), and the runner `Dockerfile` (a baked git identity).
  The no-key posture is pinned by `tests/unit/test_privmut_no_key.py`. Live-enable (the token secret + the
  gated deploy + verify) stays the operator's, on the VM — docs/privileged-mutation-enablement.md §5.

### feat(capability) — Capability-track Phase 8: backup as instance #2 (the zero-spine-edit proof) (2026-06-15)
- **The generation half.** `scripts/gen-backup.py` turns each enabled class's `backup:` block
  (`{capable, schedule}`) into `config/semaphore/schedules.generated.yml` — one per-class schedule limited to
  the class's `inventory_group` (so each class is captured on its own cron, not one fleet-wide run). Fail-closed
  (capable-without-schedule or a role missing `tasks/backup.yml` exits non-zero); `--check` lockfile. cisco_ios
  gains the worked `backup:` block. Pinned by `tests/unit/test_gen_backup.py`.
- **The dialog instance — and the SEAM PROOF.** `capabilities/backup.yml` + `scripts/kontroll/service/backup.py`
  (build_plan/apply_plan/offerable_methods + enact builder) + `classify.suggest_backup`/`declared_backup`
  register backup as instance #2. It reuses the generic shell with **zero edit** by modeling its single
  role-entrypoint capture as the one "method" and the schedule as a closed preset-cron allow-list `param`.
  Registering it touched **zero spine file** — `promote.py`, `capability.py`, the route, the GUI shell, the
  privileged tripwire, and the generic INVARIANT D\* pins are all UNCHANGED (verified by diff); the only edits
  were the one-line `CAP_DIALOGS` render hint and the new drop-in files. `registered_capabilities()` is now
  `['telemetry','backup']`, and the registry-parametrized D\* pins auto-cover backup with **no new test code**.
  Pinned by `tests/unit/test_backup.py` (suggester pair, mapping-block write, fail-closed paths, and the
  unchanged spine proposing/promoting backup end-to-end). MVP schedule-only; retention + offsite deferred.
- **Hub removal — backup scheduling is now config-as-data.** `configure-semaphore.py` no longer hand-lists a
  single `backup-configs` template + `nightly-backup` schedule; it **iterates `schedules.generated.yml`**,
  registering one template + schedule per class (each `backup-configs.yml --limit <inventory_group>`). Every
  backup-capable enabled class (cisco_ios, fortigate, proxmox, docker_host, openwrt) now carries a `backup:`
  block with a staggered nightly cron, so the generated spec fully covers the fleet — **no coverage
  regression** when the fleet-wide schedule is replaced by per-class ones. `validate.sh` gains the
  `gen-backup --check` honesty step; `modules/README.md` documents the self-describing capability blocks
  (`metrics:`/`dashboards:`/`backup:`). The live Semaphore registration is the operator's deploy step.

### feat(capability) — Capability-track Phase 7: build the generalized secondary-capability seam + telemetry as instance #1 (2026-06-15)
- **Registry foundation.** `capabilities/<cap>.yml` is the secondary-capability descriptor registry; a new
  `catalog.load_capabilities()` (the 5th sorted-glob loader) + `catalog.registered_capabilities()` load it.
  The shipped `capabilities/telemetry.yml` (instance #1) parameterizes the shared shell — every field points
  at an artifact that already exists (the telemetry vector/registry/generators), so registering telemetry
  rewrites nothing. `paths.CAPABILITIES_DIR` + `capabilities/README.md` (the "add an X" catalog). Pinned by
  `tests/unit/test_capability_registry.py`: the registry is a true drop-in, the descriptor carries the full
  dispatch contract, and — the key honesty pin — its suggester/vector/generator references resolve to real
  callables and files (never vapor).
- **The propose-then-promote gate.** `scripts/kontroll/service/promote.py` — `plan_token(*parts)` hashes a
  plan's full write-set; `verify_token` re-hashes at promote time and refuses (409) on any drift, so a promote
  only ever lands the exact bytes the operator was shown. PURE and capability-neutral (telemetry's and backup's
  parts flow through the identical function — the "promote.py verbatim" guarantee); a consistency/anti-drift
  gate, NOT an authorization control (that stays `require_token` + the fail-closed audit). Pinned by
  `tests/unit/test_promote_token.py` (purity, part-boundary identity, drift→False, malformed-token→clean-miss).
- **Picker offerability filter (`applies_when`).** Each `telemetry/<method>.yml` now declares an optional
  `applies_when` predicate; `classify.applicable_methods(facts)` returns the methods a device may be offered in
  the Stage-1 picker, reusing `predicate.eval_pred` (no new grammar). snmp → CLI/NETCONF network gear; pve →
  the Proxmox collection (`proxmox_kvm` signal); host_node + blackbox stay universal (no `applies_when`).
  Permissive offerability, not a gate — the picker unions it with already-declared methods, and an unknown
  signal keeps a method visible. Inert to the generators (byte-identity preserved; `--check` green). Pinned by
  `tests/unit/test_applicable_methods.py` (over- and under-offering both guarded).
- **The telemetry instance write (`observe.py`).** `scripts/kontroll/service/observe.py` is instance #1's
  telemetry-specific half: `build_telemetry_plan` (PURE propose — validates onboarded/method/params/idempotence,
  computes the `metrics:`/`dashboards:` add + `plan_token`), `apply_telemetry_plan` (the comment-PRESERVING
  line-surgery write — never a safe_dump flatten — + a self-protecting regen that no-ops off the real tree), and
  `telemetry_enact_commands` (the operator hand-off STRINGS; the API runs no play). The config-injection
  allow-list guard runs in the propose path (defense-in-depth with the generator). Pinned by
  `tests/unit/test_observe.py` (purity, comment-preservation, ADD-idempotence, injection-rejection, token gate,
  enact-is-strings).
- **The neutral orchestration spine (`capability.py`).** `scripts/kontroll/service/capability.py` drives the
  suggest → propose → promote lifecycle over a descriptor with **zero `if cap==…` branch**, via two
  import-by-convention seams both derived from the descriptor: the SUGGESTER (`suggest_<cap>`/`declared_<cap>`,
  read-only) and the SERVICE (`build_plan`/`apply_plan`, the instance's write half — `observe.py` for
  telemetry). `promote()` re-computes the plan and refuses ('drift') unless the token still matches, then
  applies; the token is verified over the plan's generic `token_parts`, so telemetry and (future) backup flow
  through the identical function. The descriptor gains a `service:` block; `suggest_telemetry` now sources its
  picker hint from the descriptor (default kept for the onboard nudge, pinned equal). Registering a capability
  adds an instance module, never edits this spine. Pinned by `tests/unit/test_capability_service.py`
  (dispatch, propose-is-pure, promote-applies, stale-token-refused, unregistered-degrades, hint-no-drift).
- **The HTTP surface + the registry-driven tripwire.** `api/routes/capability.py` is ONE privileged route
  (`/capability/{cap}` + `/suggest`) serving every capability via a path segment — propose (pure, returns a
  token) / promote (token-gated, audited `capability-promote`, scoped commit, never a credential) / suggest
  (read-only detection). `Catalog.capabilities` loads the registry at startup; `ratelimit._PRIVILEGED` gains
  the shared `/capability` prefix, and the tripwire test is now **registry-parametrized** — every
  `registered_capabilities()` route (and any future drop-in) is asserted privileged, so a new capability can
  never escape the token budget. Pinned by `tests/integration/test_api_capability.py` (fail-closed, 404s,
  suggest, propose-pure, promote-scoped-commit+audit, stale-token→409) + the extended ratelimit tripwire.
- **The GUI dialog (`openCapabilityDialog`).** `gui/templates/index.html` gains the standalone secondary-
  capability modal: a clickable telemetry badge opens it, the Stage-1 picker offers the methods the class can
  add (with the closed param allow-list), Propose renders the pure plan + enact commands, Promote is
  token-gated. ONE shell for every capability (`cap` is data — `CAP_DIALOGS` is a one-line render hint, backup
  joins at Phase 8). `gui/app.py` gains the thin Flask routes (`/api/capability/<cap>` + `/suggest`) over the
  spine; the dialog's `cap-*` testids are recorded in `tests/testid_reference.md`. Pinned by
  `tests/integration/test_gui_api.py` (suggest / propose-pure / promote-commits+audits / stale-409 / 404) and
  `tests/e2e/test_capability_flow.py` (the rendered open → pick → propose → close flow).
- **INVARIANT D\* pins (`tests/unit/test_invariant_d.py`).** The seam's safety property — no secondary
  capability can gate onboarding — is now test-pinned. A PRIMARY positive-allow-list pin asserts onboarding
  writes only its own known module.yml keys, structurally excluding every capability block WITHOUT iterating
  the registry (so it survives an empty/broken registry — it can't vanish). SECONDARY registry-parametrized
  pins assert onboarding writes no registered capability's block and that no suggester mutates the repo, with a
  min-count guard so an empty parametrization is RED, not vacuously green. `build_onboard_plan` is hardened to
  make the telemetry nudge **best-effort** — a suggester that raises degrades to no hint, never a failed
  onboard (a clause of D\*, pinned).

### docs(capability) — generalize the observability dialog into a reusable secondary-capability seam (2026-06-15)
- The standalone observability dialog is recast as **instance #1** of a generalized **secondary-capability
  dialog seam** (design of record: `docs/observability/secondary-capability-dialog.md`). A frozen
  `capabilities/<cap>.yml` descriptor (loaded by a new `catalog.load_capabilities()`) parameterizes ONE shared
  shell — `openCapabilityDialog(cap, key, …)` + `/api/capability/<cap>` + `promote.py` verbatim — with **zero**
  `if cap==…` branching; the suggester is resolved by import-by-convention (no god-map), so adding a capability
  never edits `classify.py`. Decision: **EXTEND-AS-TEMPLATE** (the spine is design-only + ~90% agnostic — zero
  migration cost), not a rebuild.
- **Capability-track Phase 7** recast to build the generic seam + register **monitoring as instance #1**;
  **Phase 8** registers **backup as instance #2** (a new `backup:` block + `gen-backup.py` → a Semaphore
  schedule spec consumed by `configure-semaphore.py`, removing a hand-listed hub; MVP schedule-only, offsite +
  retention deferred). INVARIANT D generalized to **INVARIANT D\*** (no secondary capability gates onboarding)
  with a positive-allow-list primary pin + a registry-parametrized secondary pin (min-count-guarded).
- PLAN.md §2.5 gains the secondary-capability "add an X" row; §10/§13 + the master §6 + `gui-actuatable-flow.md`
  (now instance #1) + `delivery-runbook.md` reconciled. SECURITY.md gains the device-capture / offsite-backup
  deferred-risk. Produced by a 10-agent design workflow (both QA lenses reconciled).

### ci(repo) — netcanon CI parity: Python 3.14 + zizmor + workflow hardening (2026-06-15)
- The pytest matrix gains **3.14** (`.github/workflows/ci.yml` → `['3.11', '3.12', '3.13', '3.14']`), matching
  the netcanon reference's 3.11–3.14 matrix. validate/e2e stay single-interpreter (3.12), as netcanon pins its
  build/publish jobs to one interpreter too.
- **zizmor** (GitHub Actions static-analysis auditor) added as `.github/workflows/zizmor.yml` + the hybrid
  action-pinning policy `.github/zizmor.yml` (first-party `actions/*`/`github/*` may tag-pin; third-party must
  SHA-pin). Path-filtered (`.github/` changes) + weekly cron + dispatch. **Advisory** (continue-on-error,
  log output) because kontroll is a private repo without GHAS/code-scanning — netcanon uploads SARIF only
  because it has GHAS; switch to the SARIF form if GHAS is ever enabled.
- **Workflow hardening:** `persist-credentials: false` on every `actions/checkout` (ci.yml ×3, security.yml ×2);
  kontroll uses only first-party actions, so nothing needs SHA-pinning yet (the zizmor policy enforces it the
  moment a third-party action is added). Not adopted (N/A — kontroll ships no PyPI package or image): Twine,
  setuptools_scm, Trivy image scan, cosign/syft, PyPI Trusted Publishing.

### feat(observability) — Phase 4: the telemetry capability vector (detection + a read-only suggester) (2026-06-15)
- **"Is this scrapable?" is now a 4th capability vector.** `vectors/telemetry.yml` (order 4, alongside
  actuate/backup/bespoke) folds the existing predicate engine with **zero code change**: `httpapi` → yes
  (medium), `cliconf`/`netconf` → yes (low), `*_facts` → yes (low). Honestly scoped as a **proxy/API-exporter
  SUGGESTER** from collection signals — it does NOT (and cannot) detect a host-agent (node_exporter is
  OPERATOR-DECLARED). Depth-stable: it never reads `maybe`, so it can't perturb the search-parity invariant.
- **DECLARED ↔ DETECTED reconciliation (read-only, writes nothing).** `service.classify.declared_metrics_methods`
  reads which enabled module already declares a `metrics:` block for a collection (both the `{method}` registry
  and a legacy `{job, via}` schema); `suggest_telemetry` turns the vector cell into a NON-binding candidate-method
  list (snmp/blackbox/an API exporter — `host_node` is never auto-primary). `build_onboard_plan` now returns a
  read-only `telemetry` suggestion; the API `/classify` gains `telemetry_declared`; the GUI/CLI render a `T`
  telemetry badge automatically (the vector loop is generic). This is the discoverability backend for the
  Phase 7 dialog's post-onboard nudge — no actuation, no gating.
- Tests: `test_telemetry_vector.py` (loads/sorts-last, 3-state resolution, depth-stable-never-maybe) +
  `test_telemetry_suggest.py` (candidates, host_node-never-primary, declared reads both schemas); 205 green.

### feat(observability) — Phase 3b: agent-less SNMP (FortiGate) + blackbox reachability for the edge/core (2026-06-15)
- **The edge firewall joins the metrics fleet as a one-line drop-in.** `modules/fortigate/module.yml` gains
  `metrics: [{method: snmp, params: {module: if_mib}}]`; `gen-observability` emits
  `prometheus/targets/network/fortigate.generated.yml` (the device addr `192.0.2.1` +
  `__param_module`/`__param_auth` labels). The FortiGate's interface counters flow into the **existing**
  generated `network` job via the **same** `snmp-exporter` + shared `v3_kontroll` auth — **no new job, no new
  exporter, no new secret** (the same read-only `kontroll_ro` v3 user the operator provisioned on both edge
  devices). This proves the telemetry-method seam: a second agent-less device is a descriptor reference, not code.
- **Read-only, highest blast radius, never actuated.** SNMP GET reads interface counters on the edge firewall
  (the single highest-blast-radius host) and changes nothing. Verified out-of-band before wiring: the
  authenticated `if_mib` walk returns 34 interface series over SHA-1/AES-128 authPriv.
- Tests: the honesty test now asserts **both** `cisco_ios` and `fortigate` emit a `network` target while
  `openwrt` (no metrics block) stays empty — the no-block ⇒ no-target rule still guarded. `SECURITY.md` C9
  already named the firewall (written in Phase 3a).
- **Agent-less reachability via blackbox_exporter — the credential-free layer.** A new `telemetry/blackbox.yml`
  (its OWN `blackbox` job — a different exporter/metrics_path than snmp's `network`, so they can't share one) +
  a static `prometheus/exporters/blackbox/blackbox.yml` (icmp/tcp_connect/http_2xx, committed, **NO secret**) +
  a `{method: blackbox, params: {probe: icmp}}` entry on both edge devices. `gen-exporters` gained a `cap_add`
  field (`[NET_RAW]` for the ICMP raw socket — the only container capability any exporter needs). Blackbox
  needs **zero device-side provisioning** (no creds, no agent) and disambiguates faults: "reachable but
  SNMP-silent" becomes a signal instead of a manual ping. `SECURITY.md` C9 gains a blackbox bullet. **Next:**
  Phase 4 (telemetry capability vector) / Phase 7 (the standalone observability dialog).

### docs(observability) — design revision: standalone, never-gates-Ansible observability dialog (2026-06-15)
- **The GUI observability opt-in is recast as a standalone, anytime per-device/service dialog** — no longer a
  section of the onboard card (Phase-7 design-only). Three entry points (a subordinate card button, a render-only
  post-onboard nudge, an anytime by-key picker over `GET /api/onboarded`), **none at create**; the dialog's own
  propose/promote control labeled "Commit config (won't start monitoring yet)"; an honest `observe-service-deferred`
  state until the service root lands. **One** root-agnostic dialog for device + service (the per-root divergence is
  one inquiry query-param branch, not a second screen).
- **Onboard↔observe decoupling pinned as INVARIANT D** (`docs/observability/gui-actuatable-flow.md` §0.1):
  observability can never gate, block, delay, or complicate making a device Ansible-usable — 16 coupling paths
  enumerated (8 structurally impossible, 8 guardrailed), with `test_decoupling_invariant.py` +
  `test_decoupling_api.py` as same-commit pins (incl. the nudge keying on a NEW `/api/onboard` `ok` field, not the
  apply-echoing `applied`, and **zero** new synchronous I/O on the onboard response path). `/observe`,
  `/telemetry/*`, propose-then-promote, and the PROPOSE/ENACT boundary are unchanged; the spine +
  `two-root-onboarding.md` reworded so `attach_observability` is observe-path-invoked, and the `load_telemetry()`
  loader-alias reconciled (§4.0.1). Produced by a 10-agent design workflow (trail in the run transcript).

### feat(observability) — Phase 3a: agent-less SNMP monitoring of the Cisco core switch (closes gap-d) (2026-06-15)
- **The switch is monitored without an agent.** A new `telemetry/snmp.yml` proxy-exporter method (SNMPv3
  authPriv, read-only) + a `modules/cisco_ios/module.yml` `metrics: [{method: snmp, params: {module: if_mib}}]`
  block make `gen-observability` emit `prometheus/targets/network/cisco_ios.generated.yml` (the device addr +
  `__param_module`/`__param_auth` labels), `gen-prometheus-jobs` emit the `network` job (the inline job left
  `prometheus.yml`), and `gen-exporters` emit the `snmp-exporter` container — all **descriptor drop-ins, no hub
  edit** (closes gap-d for the switch).
- **A new scoped secret + an injection guard.** The SNMPv3 creds live in a new `snmp_observability` SOPS domain
  (control + break-glass only, **not** the Semaphore key); `deploy-stack` renders them `no_log` into
  `prometheus/exporters/snmp/snmp.yml` (0600, **gitignored** — only the `.j2` template is committed; a
  `tests/validate` check enforces it stays untracked). The per-class `module` param is a **closed allow-list**
  (a value not in `allowed` is rejected at generate time, never interpolated). The exporter publishes **no host
  port**; SNMP GET is **read-only** (the switch is the highest-blast-radius device — monitoring actuates nothing).
- Tests: the honesty test **flips** (cisco emits a target; openwrt/fortigate stay empty) + a param-injection
  rejection test + an snmp-exporter no-host-port test; `validate.sh` gains the gitignored-snmp + no-host-port
  checks. `SECURITY.md` C9 + `ansible/secrets/README.md` + `.sops.yaml` synced. **Operator-run:** provision the
  `kontroll_ro` v3 user on the switch/firewall, `deploy-stack`, verify SNMP reachability. **Phase 3b:**
  FortiGate + blackbox.

### feat(observability) — Phase 2b: generate the exporter container from the descriptor (2026-06-14)
- **The exporter container left the hand-authored compose fragment.** `scripts/gen-exporters.py` renders
  `docker/services/<container>.generated.yaml` from each proxy-exporter method's `exporter:` block (`image`,
  `container_name`, `env`, `access_chain`, optional `config_mount`) — so adding a proxy exporter (snmp/blackbox
  next) is a `telemetry/<name>.yml` drop-in, with the compose `include:` line the only hub touch. The
  hand-authored `docker/services/pve-exporter.yaml` is removed; `docker/compose.yaml` includes
  `pve-exporter.generated.yaml`; the rendered fragment is functionally equivalent (image/env/network, **no host
  port**).
- **No exporter publishes a host port** (kontroll-net-only; SECURITY.md C3 — a blackbox/snmp exporter with a
  host port + caller-influenced `?target` is an SSRF surface): a new `tests/validate.sh` step greps every
  `docker/services/*exporter*.yaml` for a `ports:` key and fails closed. `gen-exporters --check` keeps the
  fragment honest; `deploy-stack` regenerates it before bring-up.
- Tests: `tests/unit/test_gen_exporters.py` (fragment generation, the no-host-port invariant, proxy-method
  -without-`exporter:`-block fails loud, `--check`). The generated fragment's compose validity is gated by CI's
  `docker compose config`. No new secret, no trust-boundary change. The descriptor-driven `.env` secret-render
  loop (§4.0.6) moved to **Phase 3**, where the SNMP community is the first new secret to exercise it.

### feat(observability) — Phase 2a: generate the proxy-exporter Prometheus job from the descriptor (2026-06-14)
- **The pve relabel left `prometheus.yml`.** `scripts/gen-prometheus-jobs.py` derives the `proxmox` scrape job
  (metrics_path + the multi-target relabel that proxies the device address onto `pve-exporter:9221`) from
  `telemetry/pve.yml` into `prometheus/jobs.d/proxmox.generated.yml`, included via `scrape_config_files`.
  `prometheus.yml` is now **composition-only** — adding a proxy exporter (snmp/blackbox next) is a
  `telemetry/<name>.yml` drop-in, not a `prometheus.yml` edit (**closes gap-b**). The include glob is relative
  to the config dir, so it resolves at host lint time (`prometheus/jobs.d/`) **and** in the container
  (`/etc/prometheus/jobs.d/`); the committed generated file keeps the glob non-empty.
- Tests: `tests/unit/test_gen_prometheus_jobs.py` (job derivation + relabel, host-agent-emits-no-job,
  two-methods-one-job fails loud, `--check`); `tests/validate.sh` gains a `gen-prometheus-jobs --check` step;
  `deploy-stack` regenerates `jobs.d/` before bringing Prometheus up. The generated job parses as valid scrape
  config; CI's `promtool check config` gates the `scrape_config_files` wiring. No new secret, no trust-boundary
  change (config-as-data from operator-owned descriptors; the config-injection guard lands with Phase 3
  params). **Phase 2b** (exporter-container generation + the `.env` render loop) is next.

### feat(observability) — Phase 1: the telemetry-method registry (node_exporter demoted to a drop-in) (2026-06-14)
- **`via: host|proxy` is no longer a hardcoded `if/else`.** A new **telemetry-method registry**
  (`telemetry/<name>.yml`, loaded by `catalog.load_telemetry()` via `paths.TELEMETRY_DIR`) describes HOW a
  class is scraped; a module's `metrics:` entry now **references a method by name** (`- {method: host_node}` /
  `- {method: pve}`), and `scripts/gen-observability.py` **dispatches over the registry** instead of branching
  on `via`. Adding a new ingestion protocol is a `telemetry/<name>.yml` drop-in — **not** a generator edit —
  and `node_exporter` is now **one descriptor row** (`host_node`), not the default branch.
- Seeded `host_node` (host-agent) + `pve` (proxy-exporter), which reproduce today's scrape output
  **byte-for-byte** (`git diff prometheus/targets/` empty after regeneration; the live 5/5 node_exporter hosts
  + pve-exporter see no change). The dispatcher is **fail-closed**: an unknown/missing method, an unsupported
  `kind`, a legacy `{via,port}` entry, or two methods sharing a job on one class all exit non-zero.
- Tests: `tests/unit/test_telemetry_methods.py` (registry load + sort, the node_exporter-is-one-row demotion,
  and 3 fail-closed guards); the existing `tests/unit/test_gen_observability.py` stays green **unchanged**
  (proves byte-identity). Docs: `telemetry/README.md`, `modules/README.md`, `prometheus/README.md`,
  `docs/observability/telemetry-method-registry.md`. This is **Phase 1** of the Option-A design
  (`docs/observability-onboarding-flow.md`); proxy-job generation, snmp/blackbox, the telemetry vector, the
  service root, and the GUI flow are the gated later phases. No new secret, no trust-boundary change.

### docs(observability) — Option-A design: the GUI-actuatable observability + onboarding flow (2026-06-14)
- **Designed, not built.** The full architecture to make per-device/service observability **modular + GUI-actuatable**:
  promote the hardwired `via: host|proxy` enum in `gen-observability.py` into a **drop-in telemetry-method registry**
  (`telemetry/<method>.yml` — node_exporter demoted to one descriptor row, not the generator's default branch);
  generate proxy-exporter Prometheus jobs + exporter containers from data (closing the **agent-less** gap for the
  Cisco core switch + FortiGate firewall via snmp/blackbox, read-only pull); add a **telemetry capability vector**
  (a 4th vector); converge the **two onboarding roots** (Galaxy devices + OpenAPI services, e.g. radarr/exportarr);
  a **hybrid dashboard-discovery** UX (curated-by-method + live grafana.com search); and a **propose-then-promote**,
  fail-closed, audited GUI/API `/observe` flow (enact stays delegated to Semaphore/`deploy-stack` — the network
  surface proposes data only, runs no Ansible/Docker).
- Produced by a 20-agent read-only workflow (ground → self-review → design → adversarial QA → synthesize). The
  assembled design was QA-revised and reconciled into a binding **§4.0 SHARED CONTRACTS** (one loader name, one flat
  descriptor schema, one clean-cutover dispatcher, the `network` job-vs-inventory-group fix, the owned `.env`
  secret-render) + a dependency-strict **phased roadmap**: **Phase 1** = the telemetry-method registry + host-agent
  dispatcher (byte-identical output, crosses **no** trust boundary); proxy/snmp/vector/service/dashboard/GUI deferred
  + gated. Master design: `docs/observability-onboarding-flow.md` (+ 7 section docs under `docs/observability/`);
  self-review + QA trail: `docs/reviews/2026-06-14-option-a/`. **Nothing actuated; baseline HEAD `63de129`.**
- **Operator steer (2026-06-14):** the exporter secret-render is **loop-generated** from each method's
  `secret_domain`/`secret_env_map` (§4.0.6, the future-proofed/modular option — adding an exporter secret is a
  descriptor drop-in, not a `deploy-stack.yml` hub edit); section docs are reconciled to §4.0 **just-in-time per
  phase** (the Phase-1 keystone section carries a precise supersede note now). Section-doc agent preambles stripped.

### feat(metrics) — node_exporter role: host metrics, the installable half of the observability paradigm (2026-06-14)
- **The `via: host` half of observability is now actuatable.** A new **OS-aware** `node_exporter` role
  (`prometheus-node-exporter` — apt+systemd on Debian, apk+OpenRC on Alpine, dispatched by `ansible_os_family`;
  idempotent, reversible) + `playbooks/install-node-exporter.yml`
  install the host metrics agent on the metrics-eligible classes (the ones whose `module.yml` declares
  `metrics: via: host` — proxmox/hypervisors, docker_host/docker_hosts), so the **generated `node` scrape
  targets go from DOWN → UP** and the Node Exporter Full dashboard populates. Backfill the fleet
  (`--check --diff` first) or scope to one host (`-e target=<host>`). This is the installable primitive the
  **onboarding observability opt-in** (the next step) triggers for new `via: host` devices — `via: proxy`
  classes (pve-exporter/snmp) install nothing, and classes with no `metrics:` block aren't offered it. The
  agent is additive + read-only (`:9100`, host internals — sensitive not secret, mgmt/server-VLAN-only, never
  WAN; SECURITY.md C3) and carries no credential. Docs: `roles/node_exporter/README.md`, SECURITY.md.
- **Backlog:** *onboard composed services via their OpenAPI specs* (`docs/api-architecture.md §11`) — point
  the existing `build_openapi_recipe` ingester outward at the spec-publishing services kontroll composes
  (Grafana product API, Semaphore, …), "a service is a device whose API you integrate." (The grafana.com
  dashboards registry is NOT spec-published, so the dashboard fetch stays a vendored, contract-tested path.)

### feat(dashboards) — Grafana dashboards sourced from the Grafana.com registry, declared per module (2026-06-14)
- **Grafana dashboards are community-sourced, not hand-authored.** A device class declares its data-relevant
  Grafana.com dashboard ids in `modules/<key>/module.yml` (`dashboards: [{gnet, name}]`), and
  `scripts/fetch-dashboards.py` pulls each from **grafana.com/grafana/dashboards** (latest revision), pins
  the Prometheus datasource (fixed uid `prometheus`), strips the import metadata + nulls the id, and writes
  the provisioning JSON — committed + pinned (reproducible). The dashboard analogue of `gen-observability.py`
  + Ansible Galaxy: declare the class, get the right battle-tested dashboard, zero hand-authoring. Seeded:
  proxmox → "Proxmox via Prometheus" (gnet 10347, 14 panels) + "Node Exporter Full" (1860, 31 panels);
  docker_host → 1860 (shared, fetched once). Both datasource styles handled (the `${DS_PROMETHEUS}` import
  and the `type: datasource` template variable). This is the curated/pinned tier; the **onboarding
  live-search pick-list** (grafana.com search at device-add, the operator's user-optional step) rides the
  same fetch path next. Tests: `tests/unit/test_fetch_dashboards.py` (the module-join + datasource munge; the
  network fetch is live-verified). Docs: `modules/README.md`, `dashboards/README.md`.

### feat(metrics) — Phase-5 metrics stack up: Prometheus + Grafana + pve-exporter (2026-06-14)
- **The metrics stack is deployed.** Prometheus + Grafana + prometheus-pve-exporter come up via
  deploy-stack; pve-exporter scrapes Proxmox VM/LXC/storage/cluster metrics through the PVE API using the
  **existing read-only `*.Audit` token** — **no new secret** (verified the token drives `/cluster/resources`
  → 200). The proxmox scrape now uses the correct multi-target **relabel** (the target host is proxied to
  `pve-exporter:9221?target=<host>`); the generated `prometheus/targets/proxmox/*.generated.yml` carry the
  device addresses (the old authored config pointed straight at `pve-exporter:9221`, which can't work —
  pve-exporter requires `?target=`). Prometheus/Grafana bind the **mgmt IP only** (`<mgmt-ip>:9090` /
  `:3002`, C3 parity with the API); pve-exporter publishes no host port (scraped over the `kontroll` net).
  deploy-stack regenerates the observability targets from modules+inventory before bring-up and renders the
  PVE token `no_log` into `.env`. Host (node_exporter) targets are generated but **DOWN until node_exporter
  is installed** on the hosts — the next step (an onboarding-integrated, `--check`-first role). Docs:
  `SECURITY.md` (metrics posture: mgmt-bound, read-only, no new secret), `docker/README.md`,
  `prometheus/README.md`.

### feat(metrics) — observability is GENERATED from the device modules (gen-observability) (2026-06-14)
- **Prometheus scrape targets are now generated from the fleet, not hand-written** — the metrics analogue
  of `gen-requirements.py`. Each device class declares its observability shape as data in
  `modules/<key>/module.yml` (`metrics:` — a list of scrape sources `{job, via, port, …}`; `via: host` =
  node_exporter on the host, `via: proxy` = a control-node exporter like pve-exporter queries it), and
  `scripts/gen-observability.py` joins enabled modules (`config/fleet.yml`) × their metrics × the inventory
  hosts → `prometheus/targets/<job>/<key>.generated.yml`. **Add a device or a host → regenerate → its
  scrape target appears, with zero hand-edited target files.** A class with no exporter (SNMP-only
  fortigate/cisco_ios/openwrt) generates nothing — no fake targets. The hand-authored
  `targets/node/hosts.yml` + `targets/proxmox/cluster.yml` are removed (superseded; the generated set also
  picks up the Alpine docker hosts, previously missing). `tests/validate` gains a `gen-observability --check` staleness
  guard (the 'generated, never hand-maintained' rule, enforced like `requirements.generated.yml`). This is
  the device-agnostic, programmatic foundation for the Phase-5 metrics bring-up and the 3b Homepage widgets
  (same `module.yml` seam → a `widget:` block, generated into the portal). Tests:
  `tests/unit/test_gen_observability.py`.

### fix(tests) — privileged-route test reads tags from OpenAPI, not Starlette route objects (2026-06-14)
- **CI was red on a version-fragile test, not a product bug.** `test_every_privileged_route_is_classified`
  enumerated privileged routes via `app.routes[*].tags`, which newer **Starlette no longer populates** — the
  runtime container bakes Starlette 1.3.1 / FastAPI 0.136.3, where that attribute is empty though the OpenAPI
  tags are intact. So the set came back empty and the hermetic suite failed in CI (the API classifies fine:
  `ratelimit._classify` is path-prefix based, and the live `/openapi.json` still carries the tags). The test
  now derives the tag set from `app.openapi()` (the stable OpenAPI contract), so it tracks the runtime. No
  product change. Follow-up: pin the API test deps to the container's versions (CI floats `fastapi>=0.110`)
  and migrate the deprecated httpx `TestClient` backend.

### feat(api) — /search returns fast SHALLOW records by default; deep classification on demand (2026-06-13)
- **`/search` is now sub-second for local discovery.** It returns **shallow** local records — modules +
  plugins read straight from the installed collection's files (`plugins/` walk + `MANIFEST.json`), **no
  `ansible-doc`** — instead of deep-probing every keyword match (the CPU-bound `ansible-doc` fan-out that
  made a broad keyword like `cisco` take ~10–20s even after the concurrency fix below). Deep capability
  classification is **deferred**: `?deep=true` (CLI `--deep`, GUI "deep" toggle) — or any `vector` filter,
  which needs exact cells — re-probes only the matched candidates. New seams `probe.shallow_from_local` +
  `catalog.local_shallow` (the fast local mirror of `galaxy_search`); the Record model already carried a
  `depth` field, so shallow records (`depth: shallow`, `?`/`maybe` cells) needed no schema change.
- **Accuracy is guaranteed, not hoped.** The shallow result is **sound** w.r.t. the full deep probe —
  *shallow ⊑ deep*: a capability cell is identical to the deep cell *except* where the one deep-only signal
  (backup via a `*_config` module's `backup:` option) decides it, and there shallow honestly reads `?`
  (`maybe`), **never** a flipped yes↔no. Two-axis proof: (a) **depth axis** — proved hermetically by a
  soundness-theorem test over the predicate signal space; (b) **source axis** — walking `plugins/`
  reproduces `ansible-doc -l` exactly (measured **zero divergence** across the network fleet:
  arista.eos/cisco.ios/fortinet.fortios/ansible.netcommon). New `galaxy.py verify-parity <kw>` verb +
  `service_search_parity` make this **repeatable** over the live fleet (exits non-zero on any unsound cell).
- Tests: `tests/unit/test_shallow_search.py` (soundness theorem, files-only prober, shallow-default vs
  deep-opt-in, vector-forces-deep, parity flags deferred-not-unsound + catches a contradiction);
  `test_service_layer.py`/`test_api.py` (`?deep`)/`test_gui_api.py` (deep toggle) ported to the new seam.
  Docs: `docs/api-architecture.md §10` (now implemented), `api/README.md`, `docs/capability-matrix.md`,
  `tests/README.md`, `tests/testid_reference.md` (`search-deep`).

### fix(api) — /search no longer hangs on a broad keyword (concurrent local probe) (2026-06-13)
- **The API `/search` no longer hangs.** Root cause (diagnosed live, not the first guess): a broad keyword
  like `cisco` matches every installed `cisco.*` collection (~10 in the baked image), and the local path
  deep-probed each (`ansible-doc`, ~2.6s) **serially** → ~26s, past the client timeout — while `/classify`
  and `/health` (no probe fan-out) stayed instant. Fix: `service_search` now deep-probes the local matches
  **concurrently** (`ThreadPoolExecutor`, 8 workers) and **caps** the fan-out at 24 (logged when truncated),
  so ~26s drops to a few seconds. Plus resilience so neither external call can hang a search: `galaxy_search`
  (`scripts/kontroll/catalog.py`) got a short **8s** timeout + degrades to `[]` (local-only); the
  `ansible-galaxy collection list` subprocess (`local_installed`) got a **30s** timeout + graceful `{}`; and
  `service_search` guards the Galaxy call. `origin=local` already skips Galaxy (confirmed with a test).
  Tests: `tests/unit/test_service_layer.py` (concurrent-capped-fanout, galaxy-timeout-degrades,
  local-only-when-galaxy-unavailable, origin-local-never-calls-galaxy).

### frontend — Caddy ACME ingress scaffold (mode B) (2026-06-13)
- **The frontend-exposure module gains mode `acme`** — a bundled **Caddy** ingress that fronts the kontroll
  services by hostname and obtains + auto-renews **Let's Encrypt** certs, so a no-proxy operator with a
  domain gets real HTTPS. `docker/services/caddy.yaml` + a **config-as-data Caddyfile** rendered by
  deploy-stack from `config/instance.yml` (`scripts/kontroll/frontend.py render_caddyfile`); deploy-stack
  appends `caddy` to the bring-up only in acme mode. Because kontroll is never WAN-exposed, Caddy uses the
  **DNS-01** challenge — the operator brings a domain, a `dns_provider`, the token (SOPS `acme.sops.yml` →
  `KONTROLL_ACME_DNS_TOKEN`, never in the committed Caddyfile), and a Caddy image carrying that provider's
  caddy-dns module. New `acme` SOPS domain (control + break-glass only, not the Semaphore key).
  **Scaffolded + hermetic-tested + compose-validated; NOT live-verified here** (no public domain; this
  instance runs `byo_proxy`). `self_signed`/`byo_proxy` run no Caddy. Docs: `docs/frontend-exposure.md`
  (new). Tests: `tests/unit/test_frontend.py` (`runs_caddy` + `render_caddyfile` + `backends_for`); hermetic
  suite green.

### API — frontend exposure as a per-instance mode (mode A: BYO reverse proxy) (2026-06-13)
- **The API's TLS posture is now per-instance config, not hardcoded** — the first slice of the
  frontend-exposure re-scope (`docs/reviews/2026-06-13-roadmap/03-redesign-frontend-exposure.md`), so a
  random operator can stand kontroll up with **zero certs/domain** (bring your own proxy, or get working
  self-signed HTTPS). `config/instance.yml` `frontend.tls_mode` (resolver `scripts/kontroll/frontend.py`)
  selects: **`byo_proxy`** — services serve HTTP, the operator's reverse proxy owns TLS, uvicorn runs
  `--forwarded-allow-ips=*` so the rate-limiter keys off the real client IP; **`self_signed`** — uvicorn
  TLS from a host cert (the zero-config DEFAULT, unchanged behaviour); **`acme`** — planned (Caddy/Let's
  Encrypt). `deploy-stack` renders `KONTROLL_API_UVICORN_EXTRA` from the mode and **skips the self-signed
  cert** in byo_proxy/acme. An unknown/empty mode fails **safe** to `self_signed` (never silently
  plaintext). This instance = **byo_proxy behind Nginx Proxy Manager**. Tests:
  `tests/unit/test_frontend.py` (10 cases). SECURITY.md C9 TLS posture reframed (self-signed is a chosen
  mode, not a gap).

### API hardening — rate-limiting (ws6, 2026-06-13)
- **The API now rate-limits itself** — a single in-process middleware (`api/ratelimit.py`) attached once in
  `create_app()` (no per-route edits): **per-IP** for the open inquiry routes, **per-token-digest** (never
  the raw token) for the privileged routes; `/health` + the OpenAPI/docs endpoints are exempt. A breach is a
  **429 + `Retry-After` enforced before auth runs** (a flood can't force probe I/O or spam the fail-closed
  audit), with a best-effort, throttled `rate-limited` audit line. It **fails open only on its own
  malfunction** — auth + audit stay fully fail-closed, so no path admits an unauthenticated/unaudited
  mutation. Limits are a deploy-time `KONTROLL_API_RATELIMIT` knob (not a secret) with a global `off`. First
  ws6 item from the 2026-06-13 roadmap research (`docs/reviews/2026-06-13-roadmap/`); SECURITY.md C9 residual
  cleared. Tests: `tests/integration/test_api_ratelimit.py` (12 cases) — 133 hermetic tests green.

### docker — metrics fragments: fix the compose-include bind-mount paths (2026-06-13)
- **The Prometheus + Grafana fragments mounted the wrong host paths.** `docker/services/prometheus.yaml`
  and `grafana.yaml` used single-`../` bind sources, which compose resolves against the *fragment's* dir
  (`docker/services/`) → the non-existent `docker/prometheus` / `docker/dashboards`; the real config trees
  live at the repo root (as `homepage.yaml` already reaches via `../../`). `tests/validate`'s `compose
  config` does **not** stat bind sources, so this was green-but-latent: a metrics `up` would have
  crash-looped Prometheus (a dir where a file belongs) and blank-booted Grafana (no provisioning). Fixed
  `../` → `../../` in both (4 lines), with the resolution rule comment-documented inline. Surfaced by the
  2026-06-13 roadmap design research (`docs/reviews/2026-06-13-roadmap/`).

### API-ification — read-only deployment (2026-06-13)
- **The API is deployed (read-only) on the mgmt VLAN.** `docker/services/api.yaml` runs uvicorn over TLS
  at `<mgmt-ip>:8444` on the kontroll runner image (now carrying fastapi/uvicorn), the repo mounted
  **read-only**, with **no token** — so the inquiry routes (search/probe/classify/health) serve and the
  privileged routes are **fail-closed (503)** by design. `deploy-stack.yml` provisions the RO content
  clone + a self-signed cert (gated on `stack_services: [api]`); the compose include + image are wired.
  Zero new secret, zero least-privilege expansion — the privileged mutations stay off until a token + a
  writable canonical clone + the age key are added (a deliberate later step). SECURITY.md C9.

### API-ification — onboard over HTTP, the actuation surface complete (2026-06-13)
- **The API can now onboard a device end-to-end** — `POST /onboard` (the deferred highest-blast-radius
  route, now built). Dry-run returns the plan; `apply: true` writes the module + drop-in host +
  fleet-enable, encrypts creds to SOPS, and commits + pushes the local canonical. Privileged (token +
  audit); **dry-run by default**; credentials are NEVER returned or logged (only their var names); a
  re-onboard with no change commits nothing; **bootstrap (install + live-verify) is delegated to
  Semaphore** (the API runs no Ansible). Completes the actuation boundary (api-architecture.md §3/§8;
  SECURITY.md C9 now covers it).
- A shared `gitio.commit_and_push` (a failed commit pushes nothing) backs both the onboard and
  capture-exception routes. Tests: `tests/integration/test_api_onboard.py` (dry-run / apply / idempotent
  / 404 / auth + the cred-never-logged property). 121 hermetic tests green, all documented.

### API-ification — GUI reads over the service layer (workstream 4, partial, 2026-06-13)
- **The GUI's read paths call the service layer in-process — `run_galaxy` retired for reads.**
  `gui/app.py`'s `/api/search` and `/api/classify` now call `service_search` / `service_classify`
  directly (the same functions the API uses), eliminating the subprocess + stdout-scrape for the read
  paths; `/api/classify` returns a STRUCTURED result instead of a scraped text blob. The browser is
  unchanged (`/api/search` returns the identical record shape). Workstream 4 of
  [docs/api-architecture.md](docs/api-architecture.md) §9.
- **onboard still shells out** to `galaxy.py onboard` (the actuation path) — its in-process migration
  lands with the deferred API onboard route.
- Tests: `test_gui_api.py` read tests now patch the service seams (`kontroll.catalog`/`kontroll.probe`),
  onboard keeps `run_galaxy`; the **e2e** (real headless browser) renders the cisco.ios card from the
  in-process service. 114 hermetic tests + e2e green, all documented; e2e run locally + in CI.

### API-ification — auth + audit boundary, low-blast privileged routes (workstream 3, 2026-06-13)
- **The API gains a fail-closed auth + audit boundary.** Bearer-token auth (`api/auth.py`,
  constant-time, fail-closed — no `KONTROLL_API_TOKEN` ⇒ privileged routes 503; bad/absent token →
  401, audited); an append-only, size-rotated TSV audit log (`api/audit.py`:
  `ts,user,ip,action,run_id,detail`, never creds) whose write is **fail-closed** — a mutation that
  can't be audited is refused (503), not performed; a `kontroll_run_id` minted per privileged
  request. Mirrors the GUI's fail-closed posture (SECURITY.md C8), extended to a token service as **C9**.
- **Low-blast-radius privileged routes** (the chosen increment): `POST /refresh` (rebuild the cache),
  `POST /capture-exceptions` (add + commit + push the canonical, match-validated, idempotent),
  `GET /audit/log` (query the audit). The highest-blast-radius mutation (onboard `--apply` over HTTP)
  is **deferred**. Workstream 3 of [docs/api-architecture.md](docs/api-architecture.md) §3/§4/§7.
- Tests: `tests/integration/test_api_auth.py` — fail-closed (503), 401 + denied-audit, the routes
  success+audited (git mocked, throwaway matrix/cache), audit query, and audit-unwritable → 503.
  **113 hermetic tests** green, all documented; uvicorn-verified (401 over real HTTP). Still not deployed.

### API-ification — typed FastAPI scaffold + read-only routes (workstream 2, 2026-06-13)
- **A typed FastAPI (`api/`) now stands over the service layer.** `create_app()` + a lifespan that
  loads the drop-in catalog once; read-only inquiry routes — `GET /search`, `/probe/{collection}`,
  `/classify/{collection}`, `/health` — each a drop-in router that calls a `service_*` fn **directly**
  (no subprocess-scrape). Pydantic models give an auto `/openapi.json` + `/docs`; not-installed → 404.
  Workstream 2 of [docs/api-architecture.md](docs/api-architecture.md) §2.
- **Dogfooding:** the emitted schema is a real OpenAPI 3 doc, so `galaxy.py openapi` ingests
  kontroll's OWN API (a test pins the round-trip — the ws1 ingester reading the ws2 schema).
- Tests: `tests/integration/test_api.py` drives the app with FastAPI TestClient, the service seams
  patched in their home modules — offline, 10 tests; **104 hermetic total**, all documented. Booted
  under uvicorn + verified over real HTTP. CI covers `api/` in the matrix (fastapi/httpx added to the
  dev deps; `--cov=api`).
- **Not deployed yet:** read-only + no auth — mutation routes + token auth + audit (ws3/ws4) precede
  any live exposure; the API stays mgmt-VLAN-only, never WAN (SECURITY.md C3).

### API-ification — service-layer extraction (workstream 1, 2026-06-13)
- **`scripts/galaxy.py` is now a thin CLI over a `scripts/kontroll/` service package.** The
  `cmd_*` functions compute nothing themselves — they call domain service functions that RETURN
  structured data (records, plans, recipes) and render it. The package is the drop-in-per-domain
  layout the doctrine demands: core modules (`paths`/`predicate`/`probe`/`catalog`/`record`/`gitio`)
  + one `service/<domain>.py` per CLI verb (search/probe/classify/refresh/audit/scaffold/onboard/
  openapi/capture_exception). Behaviour-preserving — CLI output is **byte-identical** (parity-verified
  against HEAD) — the keystone for the typed API ([docs/api-architecture.md](docs/api-architecture.md) §1).
- The onboard service splits into `build_onboard_plan` (pure — returns the plan) + `apply_onboard_plan`
  (the repo mutations), the API-ready dry-run/apply shape; the commit/push gate stays in the CLI (the
  capture-exception exemplar's boundary). Credential values flow through the plan in memory only —
  never rendered into the host file or logged (pinned by a new test).
- **Test seams moved to their home modules** — patch `kontroll.probe`/`kontroll.catalog`/`kontroll.gitio`/
  `kontroll.paths`, not `galaxy`; the call-style pure-function names still resolve off `galaxy`
  (re-exported, back-compat). New `tests/unit/test_service_layer.py` drives the services directly (no
  stdout-parsing — the cleaner shape the extraction unlocks). 94 hermetic tests green, all documented.

### Phase 1 — credentials & connectivity (2026-06-13)
- Scoped Proxmox API token (`<user>@pve!<token-id>`, PVEAuditor) + FortiGate API
  token + Cisco login, all **SOPS/age-encrypted** in `ansible/secrets/{proxmox,network}.sops.yml`;
  wired into group_vars via the `community.sops` lookup.
- Dedicated `ansible` SSH key generated by the bootstrap; installed on the fleet.
- `ping.yml` reaches the entire live fleet (8/8) — the OpenWrt AP came online once
  the mgmt→AP-VLAN firewall rule was opened; verified out-of-band (ICMP + TCP/22 +
  a real `raw` SSH board-name read), not just by the playbook's own probe.
- Break-glass second-recipient flow documented in SETUP.md §5 + a bootstrap nudge.
- A retired hypervisor removed from inventory + Prometheus targets.

### Local source of truth — git optional for the operator (Phase A)
- **The running instance is now the source of truth; GitHub is optional.** Semaphore
  clones a **local bare repo** (`/srv/kontroll.git`, mounted read-only at
  `file:///srv/kontroll.git`) instead of GitHub — the control plane needs no remote git
  to operate. `playbooks/local-canonical.yml` (idempotent; imported by deploy-stack)
  creates + seeds the bare repo and sets the working tree's `local` remote.
- **`onboard` commits push the local canonical** so Semaphore sees a new device with
  zero GitHub; `--push` is demoted to an **optional offsite backup** to `origin` (no
  longer required to operate — this also dissolves the write-key trust gate).
- Semaphore env gains `safe.directory=*` (the bare repo is operator-owned, runner-uid
  cloned) — avoids an image rebuild. The github-deploy key stays for the optional
  backup remote. Plan of record: [docs/local-source-of-truth.md](docs/local-source-of-truth.md).
- **Phase B — code/state separation, no-clobber updates.** The code/state boundary is
  now explicit (`config/state-manifest.yml`: inventory drop-ins, group_vars, secrets,
  API recipes, fleet selection, dashboards = state; everything else = code), and the
  instance **never `git reset --hard` again**: `scripts/update.sh` pulls upstream code
  and **merges** it, preserving instance state (disjoint files → conflict-free; a real
  conflict means a code file was edited locally — surfaced, not lost), then re-seeds the
  local canonical. Kills the Phase-A caveat (a dev code-push clobbering operator state).
  Code-physically-from-the-image (full git-free operator updates) folds into Phase C.
- **Phase C (partial) — pluggable offsite backup.** `scripts/backup.sh` mirrors the
  instance (code+state) to any configured git remote — **opt-in, never required to
  operate**. Each target uses its own **single-repo, strictly-scoped write deploy key**
  (bound to exactly one repo by GitHub, `IdentitiesOnly`-pinned, generated on the VM).
  Provisioned + verified against `netcanon/kontroll-prod-test` (an example offsite
  target): the key authenticates **only** as that repo and is **denied** on every other
  (SECURITY.md **C7** — git access is least-privilege, per-repo).
- **Phase C (cont.) — non-git state snapshot / DR.** `scripts/state-snapshot.sh` tars
  exactly the `state-manifest.yml` paths into a portable archive (restore anywhere with
  no git via `state-restore.sh`: base code ⊕ restored state). Verified: the archive holds
  only state (secrets/inventory/fleet/recipes/dashboards) — **zero code** — and
  round-trips clean; the age key is excluded by design, so a leaked snapshot decrypts
  nothing. This is the DR owner now that the instance, not GitHub, is the source of truth.
- **Phase C (cont.) — git-free install.** `scripts/make-bundle.sh` builds a release
  bundle (product code at HEAD, minus secrets + onboarded hosts); `scripts/install.sh`
  stands a fresh instance up **from the bundle with no GitHub clone** — `git init` a
  local working tree (no remote), then the existing bootstrap/deploy flow. Verified in a
  sandbox: clean bundle (no secrets/state), installed tree is a git repo with **zero
  remotes**, full code, and seeds a Semaphore-cloneable canonical — all git-free.
  Replaces the `git clone` step in SETUP §2; the operator needs no git/GitHub account.
  **The instance is now fully git-optional end to end** (Phases A–C): operate, onboard,
  update, back up, and install — none require a remote. *Optional future polish:* code
  baked into the image, and bundle genericization for public distribution.
- **Phase D — security + persona.** Trust boundary re-anchored on where state lives, not
  a remote: SOPS everywhere state flows + filesystem perms + leak-proof bundles/snapshots
  (SECURITY.md **C8 — local state at rest**, alongside C7). `source_of_truth` is a real
  knob (`config/instance.yml`: the posture **+** the offsite `backup_remotes` list that
  `scripts/backup.sh` now consumes — pluggable, multi-target). Two personas made explicit
  (operator = git-free; developer = git-connected) in the design doc + SETUP §2.

### Hardening — secret-leak gate
- **gitleaks secret scan** wired into `tests/validate` (`.sh` + `.ps1`) and a
  `.pre-commit-config.yaml` hook — catches a plaintext key/credential in any committable
  file **before** it can reach a commit / the canonical / a backup / a bundle / a
  snapshot (the new risk surface from local-source-of-truth). `.gitleaks.toml` allowlists
  SOPS ciphertext + the gitignored `local/` + deploy-rendered `docker/.env`, with a
  **custom rule that always catches an `AGE-SECRET-KEY`** (the crown-jewel secret).
  Bootstrap installs gitleaks (pinned 8.18.4). **Verified:** the gate is clean on the
  repo and **catches a planted age private key** (exit 1). First slice of the QA/CI plan
  (docs/qa-and-release-pipeline.md §3/§4); SECURITY.md C6 upgraded.
- **CI in GitHub Actions.** `.github/workflows/ci.yml` runs the **exact same
  `tests/validate.sh` gate** as local + the VM (single source of truth for green),
  hermetically — the live lab is never in CI. Collections are resolved from `modules/`
  via the new standalone `scripts/gen-requirements.py` (no hardcoded list). `security.yml`
  adds a gitleaks **full-history** scan + pip-audit; `.github/dependabot.yml` keeps pip +
  actions + docker deps current. The offline gates now run on every push + PR.
  Actions pinned to Node-24 (`checkout@v6`/`setup-python@v6`, clearing the Node-20
  deprecation); upstream toolchain deprecation noise (ansible-core's internal
  `disable_lookups`, pathspec) quieted in the CI log only — not our code, and
  ansible-lint's own deprecation rules still guard ours.
- **pytest suite for the code half (`scripts/galaxy.py` + the GUI).** `tests/unit`
  (three-valued predicate engine, backend classify, record/overrides, comment-preserving
  fleet-edit, OpenAPI heuristics) + `tests/integration` (onboard dry-run/`--apply`
  idempotence, search pipeline + `--vector` filter, the Flask auth gate + `/api/*`).
  **Fully offline** — the three shell-outs (`ansible-doc`/`ansible-galaxy`/Galaxy urllib)
  and the GUI's `run_galaxy` are mocked; file-mutating paths run against a throwaway repo —
  so it needs neither ansible nor the lab. No production code changed (the seams already
  existed). Markers + config in `pyproject.toml`; `tests/validate` runs it as L2
  (skip-if-absent) and `ci.yml` adds a `tests` matrix job (py3.11–3.13). This closes
  qa-and-release-pipeline.md §7 items 1–2. A real heuristic quirk surfaced + pinned: the
  OpenAPI check-endpoint regex matches `/system` inside a backup path (STAGED output,
  verify-before-trust tolerance).
- **Logging Layer 0 — bound the unbounded surfaces (no new services).** Container logs now
  rotate via a **global Docker daemon default** (`/etc/docker/daemon.json`: `json-file`
  10m × 5, set by `deploy-stack.yml`) — one place bounds every container so a crash-loop no
  longer rolls its own evidence off disk, and a newly added service inherits it for free.
  **Verified live:** a 55 MB emitter capped at 5 files × ~10 MB vs a single unbounded 26 MB
  file before; note a daemon *restart* (not reload) is required to apply log-opts. The GUI
  audit log rotates via a `RotatingFileHandler` (5 MB × 5), format + cred-exclusion preserved
  (test-covered).
- **Logging Layer 0 — git-backed capture history.** `backup-configs.yml` now commits a
  timestamped snapshot of the captures dir into a **local-only git history** (no remote,
  inside the 0700/uid-1001 dir, never pushed — as protected as the captures), so device
  configs are diffable over time (`git diff` answers "what changed on this device?").
  Idempotent (no change → no commit). **Verified live** through a real Semaphore backup run
  (`/var/lib/kontroll/backups/.git` created, 7 captures tracked, a clean device-config diff).
  See [docs/logging-architecture.md](docs/logging-architecture.md).
- **Logging Layer 0 — run correlation key + script run-logs.** A shared `_log-run-id.yml`,
  imported first by the fleet playbooks (ping/backup-configs/deploy-stack), logs a
  `kontroll_run_id` at the start of every run — into the ansible log AND the queryable
  Semaphore job output — so one operation is greppable by id (synthetic UTC stamp by default;
  `-e kontroll_run_id=…` overridable). Investigated: Semaphore exposes no task-id env var, so
  an in-playbook id is the reliable mechanism (it co-appears with the Semaphore task id for
  cross-reference). And `scripts/lib/run-log.sh` (`run_log_init`) tees a script's output to a
  timestamped, 30-day-pruned log in an operator-writable dir (via `exec`-redirect — no
  pipe-subshell exit-code bug), sourced fail-soft by backup/update/state-snapshot/state-restore.
- **Capture-exception matrix — declare anomalies, don't hardcode vendors.** A live finding
  (the FortiGate full-config export is non-deterministic: ~570 non-secret lines re-serialize
  per fetch, so an unchanged config diffed ~600 lines every backup) is handled the modular
  way: a **sparse, member-only registry** (`config/capture-exceptions.yml`) — the `overrides/`
  pattern applied to capture *behaviour* — lists ONLY the captures that misbehave. The history
  play renders the captures-dir `.gitignore` from the matrix's `exclude_from_history` members,
  so a non-diffable capture is kept on disk (latest) but not versioned — no noise commits, the
  history stays meaningful for the deterministic fleet. **Verified live:** two back-to-back
  Semaphore backups → one cleanup commit, then zero further commits. Extensible by one row (by
  hand, or GUI→git ahead); instance state, so additions survive updates + DR.

- **Live smoke gate formalized.** `scripts/run-smoke-gate.sh` triggers the fleet liveness gate
  (TCP probe + each class's read-only `check` + an honest assertion — `ping.yml`, run as the
  `ping-fleet` template), polls to completion, and reports PASS/FAIL with the task id. Callable
  post-deploy or by a webhook (pass the admin password in the env for a remote caller); results
  queryable via the Semaphore task API, correlatable by `kontroll_run_id`. Reuses `ping.yml` —
  no duplicate smoke playbook (the gate already existed; this makes it one-command + pollable +
  run-logged). **Verified live:** PASS against the 8-device fleet.
- **Playwright GUI e2e in CI (headless).** `tests/e2e/test_onboard_flow.py` drives the
  onboarding GUI in a headless browser over the **real booted Flask app** — `conftest.py`
  starts `gui/app.py` in a daemon thread with `run_galaxy` mocked (no galaxy.py/ansible/lab),
  HTTP Basic via the browser context — proving the rendered UX: search → result card → fill
  the form → dry-run → the plan renders. A few `data-testid` hooks added to `index.html`
  (zero behaviour change). A dedicated GitHub Actions `e2e` job installs chromium and runs it
  headless on every push/PR; the hermetic `tests` job excludes the `e2e` marker (collects but
  deselects). **Cost: $0** within the private-repo Actions free tier. **Verified locally**
  (3/3 green headless). Mirrors netcanon's booted-server + mocked-I/O-boundary pattern.

### Hardening — onboarding GUI: auth + TLS + audit
- The privileged onboarding GUI (`gui/app.py`) is no longer an unauthenticated surface.
  **HTTP Basic auth, fail-closed** — the app *refuses to start* without `GUI_PASSWORD`
  (constant-time compare; password from the SOPS `dashboards` domain). **TLS** via
  `GUI_TLS_CERT`/`GUI_TLS_KEY` (warns + HTTP only if unset). **Audit log** of every
  onboard action + auth-denial (timestamp, client IP, collection/key/host/flags — never
  creds). The compose fragment + `.env.example` carry the wiring; deploy provisions a
  cert + the SOPS password. **Verified on the VM:** fail-closed startup, no-auth → 401,
  HTTP→HTTPS-port refused (real TLS), correct auth over HTTPS works, wrong password → 401,
  denials logged. Closes the SECURITY.md C8 GUI residual (now: self-signed cert + SSO are
  the only pre-wider-exposure items).

### Phase 4 — Homepage portal (live)
- **Homepage deployed** on the control node (`docker/services/homepage.yaml`,
  `:3000`), config-as-code from `dashboards/homepage/` (settings/services/widgets/
  bookmarks, split by concern). Serves all the configured tile groups + a bookmarks group. Added to the default `stack_services`.
- **Two deploy bugs fixed, found by live-verify (not assumed green):**
  (1) current Homepage (Next 16) force-copies skeleton config on start and crash-loops
  on a **read-only** mount (`EROFS`) — switched the config mount to read-write +
  `LOG_TARGETS: stdout`, and gitignored Homepage's generated runtime files so the
  repo stays clean. (2) The config mount path was **one level short**: Compose resolves
  an included file's relative paths from that file's own dir (`docker/services/`), so
  `../dashboards/homepage` pointed at a non-existent `docker/dashboards/homepage`
  (Homepage silently populated it with skeletons) — corrected to `../../dashboards/homepage`.
  Verified: the container mounts the real repo dir and serves our groups/bookmarks.
- **Pending (4b):** live widget data needs per-service API tokens (`HOMEPAGE_VAR_*`)
  SOPS-encrypted into `dashboards.sops.yml` + rendered into `docker/.env`; tiles/links
  work today, widgets degrade gracefully until then. External publish still pending.

### Added — capability search + onboarding pipeline (capability-matrix Phases 1–4)
- `scripts/galaxy.py` + `vectors/*.yml` — capability-aware onboarding search. One
  query spans Ansible Galaxy + locally-installed collections, each result a
  structured **record** (metadata + `capabilities` keyed by drop-in vectors +
  `suggested_backend`), **derived** by probing (`ansible-doc -j` deep for local;
  Galaxy `contents` shallow for remote) — origin/depth shown, `--vector` facets, `--json`.
- **Pipeline: search → classify → scaffold.** Drop-in execution **backends**
  (`ansible/backends/<name>/backend.yml`: netcommon_cli / vendor_config / api /
  napalm / raw_ssh) with `classify` predicates; `galaxy.py classify` picks the
  backend, `scaffold` emits a `modules/<key>/module.yml` device declaration (staged).
- **Overrides** (`overrides/*.yml`) correct the residual the prober can't see (e.g.
  FortiGate's API-only backup). `refresh` caches the derived matrix; `audit`
  reconciles enabled-module collections vs installed.
- Verified live: classifier maps cisco.ios→netcommon_cli, fortinet.fortios→api,
  community.routeros→netcommon_cli; scaffold derives `network_os` correctly.
- **`galaxy.py onboard` — the one-shot.** Dry-run prints the plan; `--apply`
  performs three idempotent repo mutations (module declaration + an additive
  drop-in inventory host `ansible/inventory/onboarded-<key>.yml` + the
  `config/fleet.yml` enable); `--commit` commits them (rationale-first + trailer);
  `--bootstrap` installs the new collection and live-verifies the host via
  `ping --limit`. Joining an existing group inherits its `group_vars` (connection +
  SOPS creds); a new group is flagged to add one. Credentials (SOPS, on the control
  VM) and the final `git push` stay **deliberate manual gates** — nothing plaintext
  or canonical happens automatically. The "click → managed device" backend a GUI calls.
- **Drop-in inventory** — `ansible/inventory` is now a **directory** inventory
  (`ansible.cfg`, and Semaphore's inventory now points at the dir). Ansible merges
  every `*.yml` source; `hosts.yml` stays the canonical migration mirror while
  onboarded device classes land as additive `onboarded-<key>.yml` files — no hub
  edit to add a host. Verified: a drop-in host joins its group and inherits
  `group_vars` (connection + SOPS creds) unchanged. Enables `onboard --apply`.
- **Execution backend `roles/backend_netcommon_cli`** — generic check/backup for any
  `network_cli` platform via `ansible.netcommon.cli_command` (no vendor module).
  `cisco_ios` migrated onto it (thin wrapper). **Non-human e2e proof** (`tests/e2e/`):
  the machine-scaffolded `modules/ios_xe_auto` class backs up the live Catalyst via
  the generic backend, capture **byte-identical** to the hand-written role's — no
  human wrote a Cisco task. `backend_*` are shared cross-backend contract vars.
- **Execution backend `roles/backend_raw_ssh`** — generic capture for bespoke SSH
  devices via a class-supplied command; `openwrt` migrated onto it (`uci export`).
  Full-fleet backup verified green, Cisco + OpenWrt captures byte-identical to the
  old per-vendor roles.
- **Execution backend `roles/backend_api`** — generic REST `check`/`backup` for
  API devices **and services**, driven by per-API **recipes**
  (`ansible/backends/api/recipes/<name>.yml`: auth + endpoints, secret-free — the
  token is named via `token_var` and resolved from SOPS vars at runtime). Plain
  `uri` delegated to localhost; no vendor module, no httpapi plugin, no collection
  required. `fortigate` migrated onto it (`fortios` recipe) — its `check`/`backup`
  are now thin wrappers. Verified live: the generic full-configuration capture is
  **byte-identical** (11234 lines / 334867 bytes) to the hand-written FortiOS role's,
  differing only in FortiOS's per-export re-encrypted secret blobs. This generalizes
  the last custom capture: **the whole fleet now runs on generic backends**, fleet
  diversity absorbed as data (recipes), not code.
- **`galaxy.py openapi` — the OpenAPI ingester.** Derives an `api`-backend recipe
  from a machine-readable spec (a live instance's `/openapi.json`, an apis.guru
  entry, or a vendor file): reads `securitySchemes`→auth, `servers`→port/base, and
  classifies endpoints by heuristic (GET `/backup|/export|/config`→backup; any
  write verb→actuate; GET `/status|/health|/ping`→check) into a **staged** recipe.
  Token/cred vars are named (wired to SOPS, never inlined); guessed auth + unmatched
  capabilities are flagged for the live-verify gate. Self-signed TLS + UA-gated
  registries handled; bad fetches fail with a clean message. Verified: a crafted
  spec exercises every branch; the live apis.guru spec reads as read-only
  (actuate/backup ✗) correctly. Recipe authorship for the API tail is now automated.
- **`onboard` self-completes the whole flow.** `--username/--password/--api-token`
  are encrypted into the secrets domain (`sops --set`, namespaced per host) and the
  drop-in host carries **inline per-host SOPS cred lookups** — self-contained, no
  `group_vars` hand-edit even for a brand-new group. `--push` makes the change
  canonical so Semaphore (which clones origin) sees the host (fails clean without a
  write-capable remote — the one deliberate trust gate). The only remaining inputs
  are the search selection and the connection details. Verified on the control node.
- **`gui/` — onboarding web surface (alpha).** A thin Flask shell over `galaxy.py`:
  search → capability badges (A/B/L + suggested backend) → "Onboard" → connection
  form → managed device + backup. Carries no logic (every action shells to galaxy.py);
  actuation is explicit (dry-run unless *apply* ticked). Verified VM-direct (page
  serves, `/api/search` returns records, `/api/onboard` drives a real dry-run).
  Packaged as `docker/services/onboard-gui.yaml` on the runner image (+flask) — that
  container path is **staged**, not yet deployed. **Privileged surface** (writes repo,
  encrypts secrets, pushes, runs Ansible): mgmt-only, no auth yet — pre-beta needs
  auth + TLS + audit log.

### Design
- **Testing/test-docs/logging codified into the process (governance).** Per the operator —
  these are what let later AI/human iterations continue development. New **`docs/testing-standards.md`**
  (the binding standard: the test-layer map, the per-test docstring rule, the data-testid SOP +
  naming, the test-after-change loop, how logging ties in) + a thin tool-agnostic **`AGENTS.md`**
  pointing at CLAUDE.md (netcanon's cross-tool convention). **CLAUDE.md** gains Hard Rules (every
  test has a docstring; every interactive/asserted GUI element has a `data-testid`; tests/test-docs/
  logging are first-class same-commit deliverables; dispatch agents on `model: opus`, not Explore's
  default Haiku), Doc-Sync rows, and Before-commit items. **`tests/testid_reference.md`** is the
  GUI selector inventory. The docstring rule is **machine-enforced**: `tests/check-test-docs.py`
  (an AST check) runs in `tests/validate` (`.sh` + `.ps1`) and fails on any undocumented test.
  Stale `engineering-standards.md` §5 corrected (it claimed Playwright/data-testid were not
  adopted — the first-party GUI now uses both). Reciprocal See-also links throughout.
- **`docs/api-architecture.md`** — plan of record for fully API-ifying kontroll: the five
  workstreams (service-layer extraction from `galaxy.py` → FastAPI over it → the actuation
  boundary → token auth/audit → tests + published OpenAPI), with testing across every layer
  (unit → TestClient → Playwright e2e → Semaphore live smoke → OpenAPI contract) and the
  Layer 0 logging/audit (run_id correlation) wired in. Honest that even netcanon is ~80%
  API-ified, and that kontroll's lift is smaller because Semaphore already provides
  execution/scheduling/RBAC (delegated, never reinvented). The `capture-exception add` CLI
  is the workstream-1 service-layer exemplar.
- **`docs/logging-architecture.md`** — plan of record for full-fidelity log
  troubleshooting. Maps the current surfaces (ansible run log, Semaphore/Postgres job
  output, overwrite-only device captures, driver-default container logs, GUI audit log,
  implicit host journald) with citations + the gaps (no rotation/persistence, no
  cross-silo correlation, no config history, no off-Semaphore run history, no alerting),
  then a layered target: Layer 0 foundations (rotation + durable run-logs + a Semaphore
  `run_id` correlation key + git-backed capture history — no new services, reversible) →
  Vector → Loki → Grafana (one pane, secret-safe, label-indexed, mgmt-VLAN only). Reciprocal
  links from engineering-standards.md §3 + PLAN.md Phase 5. (Collector DECIDED = Vector, 2026-06-16.)
- **`docs/qa-and-release-pipeline.md`** — plan of record for full CI (pytest unit +
  integration + Flask-API + molecule), security testing (gitleaks, pip-audit, bandit,
  trivy, `no_log`/sops gates, Dependabot), a test-after-change discipline (pre-commit +
  `validate` as the single gate), and automated build + public dissemination (GHCR
  signed images + SBOM, a versioned compose release bundle) once the GUI hits beta.
  Modeled on the netcanon setup, adapted for the composed/IaC shape (CI stays hermetic;
  the live lab is never in CI).
- `docs/capability-matrix.md` expanded to the full search-and-add plan: the
  onboarding ("operator's path") flow + `onboard` orchestrator (§4.1), the backend
  taxonomy (~a handful, bounded by execution paradigm, §5.1), and the **API tail —
  one recipe-driven `api` backend enriched from OpenAPI** (live `/openapi.json`,
  apis.guru, vendor schemas; §5.2). Build phases updated through GUI.
- `docs/capability-matrix.md` — design for programmatic, extensible device/service
  onboarding: one search bar over Galaxy + local results, labelled by capability
  vectors (actuate / backup / bespoke) derived from `ansible-doc -j` + plugin
  probing; pluggable execution **backends** (netcommon_cli / napalm / …) so most new
  devices become a one-line declaration. Verdict: derive locally + drop-in overrides
  (no separate AI-maintained repo needed unless a shared matrix is ever wanted).

### Phase 3 — Semaphore UI + scheduler (2026-06-13)
- `scripts/configure-semaphore.py` — idempotent, codified Semaphore setup
  (project, Key Store, repository, environment, inventory, templates, schedule).
  No click-ops. **Verified end-to-end:** Semaphore clones the repo via the
  read-only deploy key, finds the baked collections, decrypts SOPS with the
  scoped age key, and reaches all 8 fleet hosts (ping-fleet → success); the
  `backup-configs` template + a daily-02:00 schedule are live (GUI-editable).
- Scoped Semaphore age key (separate from the control-VM master key) added as a
  recipient on the `network`/`proxmox` domains only; least-privilege + keeps SOPS
  the single source of truth. Runner gained `paramiko` (Cisco `network_cli`).
- Scheduled backups **persist**: `deploy-stack` creates `/var/lib/kontroll/backups`
  (0700, uid 1001), bind-mounted into the runner; `backup-configs` writes there via
  `config_backup_dir=/backups` (Semaphore runs only). Verified — a Semaphore backup
  run lands all five captures on the host volume. (Encrypted/off-box versioning of
  backups is a deliberate future enhancement.)
- `ansible/playbooks/deploy-stack.yml` — idempotent local deploy: installs Docker
  Engine + Compose (deb822 repo, Debian 13), renders `docker/.env` from the SOPS
  `semaphore`/`dashboards` domains (`no_log`), and brings up a data-driven
  `stack_services` list (default `[semaphore]`). **Semaphore is live** on the
  control VM `:3001` (admin login verified); it will be the scheduler (no system cron).
- New SOPS domains `ansible/secrets/{semaphore,dashboards}.sops.yml` (service
  passwords + Semaphore access-key encryption), encrypted to both recipients.
- Custom Semaphore **runner image** (`docker/semaphore-runner/Dockerfile`): stock
  image + `sops`/`age` + the device-class collections baked from the bootstrap
  lockfile into ansible's default path. deploy-stack builds it; the runtime secrets
  (age key, device/deploy SSH keys) are injected via Semaphore's Key Store /
  environment by the API setup, not mounted (avoids uid-mapping).
- The shared `kontroll` Docker network is now **external**, created by deploy-stack
  (was inconsistently half-managed by compose, which broke single-service bring-up).

### Phase 2 — config backups + backup-capability (2026-06-13)
- Backup is now a **declared per-class capability**, not a universal assumption:
  each role carries `backup_capable` (`roles/<role>/defaults/main.yml`,
  overridable per host). Non-capable classes record a **logged skip** instead of a
  forced empty capture — "manage-but-don't-back-up" is a first-class, explicit
  state. `backup-configs.yml` gained a per-host result summary.
- Real read-only captures implemented for the active fleet: `cisco_ios`
  (running-config), `fortigate` (FortiOS backup API → full-configuration),
  `openwrt` (`uci export`), `proxmox` (guest `.conf` + node network/storage).
  `docker_host` is **not** backup-capable by default (app-managed state; per-host
  opt-in yields a container/stack manifest). Staged `opnsense`/`routeros` captures
  are written but unverified until cutover.
- A capture that **fails** (vs a tolerated skip/unreachable) is reported in the
  summary and fails the run (no silent green). The FortiGate full-config capture
  needs a read-write `super_admin` API profile (config backup is gated behind full
  admin on FortiOS); once granted it captures cleanly (11k-line config verified).
  All five capable classes now back up and the fleet run exits green.
- All captures are `no_log` and land in `ansible/backups/`, which is now
  **gitignored** (captures carry config hashes / PSKs / storage creds — never
  committed). `.role-template` and `modules/README.md` document the contract.

### Security — break-glass recovery key (2026-06-13)
- Every SOPS secret is now encrypted to **two** age recipients: the control-VM
  operational key + an offline **break-glass** recovery key (private half held
  off-machine). `.sops.yaml` carries both via a shared anchor; existing ciphertext
  re-wrapped with `sops updatekeys` on the VM. Removes the single-point-of-total-loss
  on the VM key — see SECURITY.md C1 + accepted-risks.

### Fixed — scaffolding review (docs/reviews/2026-06-13)
- `fortigate/check.yml` now `no_log` (was leaking the API token); `.sops.yaml`
  per-domain regexes corrected (were dead — only the catch-all encrypted);
  `.role-template` gained `check.yml` + a corrected README (the "copy the template"
  recipe was producing a class that broke `ping.yml`); all 7 `check.yml` gained
  the molecule guard; doc drift fixed (creds status, the retired node, LXC→VM, `make bootstrap`).

### Added — netcanon methodology integration
- `docs/agent-workflow.md` — the distributed-agent-work protocol (read-only
  agents write reports into `docs/reviews/<UTC-date>/`; the main thread is the
  only actor that validates + actuates; cluster taxonomy + dispatch heuristics).
- CLAUDE.md: Documentation Sync Checklist, agent-work hard rules, no-hard-coded-
  counts-in-prose rule, rationale-first commit convention.
- engineering-standards.md §2e/§2f: drift prevention + agent-work pointers.

### Added — modular config system (process-modularity)
- `modules/` device-class registry: each class is a self-describing
  `module.yml` (collections, role, inventory group, secrets domain) + `_core.yml`.
- `config/fleet.yml` — the single pick-and-choose knob (`enabled_modules`).
- `ansible/playbooks/bootstrap.yml` — idempotent, fleet-driven control-node
  bootstrap: generates the collection set from enabled modules, installs the
  toolchain + age key, verifies. Codifies the previously-manual Phase-1 setup.
- `bootstrap.sh` (Stage 0) and `docs/SETUP.md` (canonical, agent-runnable first-run
  + continual-config guide).

### Changed
- `requirements.yml` removed — the collection list is now **generated** from
  enabled modules (`requirements.generated.yml`, gitignored). Corrected
  `ansibleguy.opnsense` constraint (latest is 1.2.x, not 2.x).
- `bootstrap-control-vm.md` trimmed to the Proxmox VM-creation recipe; config
  steps moved to `docs/SETUP.md`.
- PLAN.md §2.5 + CLAUDE.md extended with the process-modularity doctrine.

### Infra
- Published to private remote **github.com/netcanon/kontroll** (verified private;
  `local/` + keys excluded). Documented the read-only deploy-key clone flow for
  the control VM in `docs/bootstrap-control-vm.md` §3.
- Control VM provisioned: a Debian 13 VM (`control`) on a Proxmox node, mgmt VLAN @
  `<mgmt-ip>` (details in git-ignored operator notes).

### Added
- Engineering standards distilled from the NetConfig project and translated to
  IaC: testing pyramid (lint → validate → live smoke → idempotence),
  documentation altitudes, logging + secret-redaction, config-as-data
  principles. See `docs/engineering-standards.md`.
- `CLAUDE.md` — contributor directives + Hard Rules (Never Break).
- `SECURITY.md` — threat model, controls (each mapped to a covering check),
  accepted-risks table, update triggers.
- `tests/` — L1/L2 validation runner (`validate.ps1` / `validate.sh`),
  `mock-inventory.yml`, lint configs (`.yamllint`, `.ansible-lint`), and a
  Molecule example for the `cisco_ios` role. `tests/README.md` documents the
  pyramid and how to add tests.
- `no_log: true` discipline applied to secret-handling tasks in example
  playbooks/roles.

### Changed
- `PLAN.md` refactored to make testing, documentation, logging, and security
  first-class: new repo-layout entries, a testing/quality phase, and exit
  criteria gated on `tests/validate`.

## [2026-06-12] — Phase 0 scaffold

### Added
- Modular control-plane skeleton: Semaphore + Homepage + Prometheus + Grafana
  compose stack (one file per service), functional Ansible inventory,
  role-per-device-class, file-based Prometheus SD, SOPS+age secrets wiring.
- `PLAN.md` (architecture, locked decisions, modularity doctrine, rollout),
  `README.md`, `docs/bootstrap-control-vm.md`.
- Current-edge targets (FortiGate 100E + Cat 9300) with OPNsense/CRS310 staged
  for a cutover membership-flip.
