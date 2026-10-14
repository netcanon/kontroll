"""canon — how the tool reads the canonical bare repository, and the one environment a git that touches it runs in (C10).

The canonical (`/srv/kontroll.git`) is the instance's source of truth: `refs/heads/main` is what runs, and each
`refs/heads/proposed/<run id>` is a change the network service staged for a human to promote. Its rules live here,
so that each is one function with one test file (tests/unit/test_canon.py) and no reader has to remember them:

  1. A REF IS NAMED IN FULL, READ ONCE, AND IS AN ID FROM THEN ON. A short name is looked up — `main` is a tag of
     that name before it is the branch, and `proposed/<id>` is `refs/proposed/<id>` before it is the proposal — so
     no short name reaches git from here. `read_ref` returns the ref's OWN value with its type and, for a symbolic
     ref, the name it points at; it never peels a tag and never treats a symbolic ref as the thing it names. What a
     caller does next, it does with the id.

  2. EVERY GIT WHOSE REPOSITORY IS THE CANONICAL RUNS IN `git_kwargs(repo)`. A repository can make git run
     programs and read other stores through its own files: a hooks directory, a `commondir`, a replace ref, a graft
     line, a `shallow` file, a commit-graph. `git_env` names the repository outright (nothing is discovered, no other
     config scope is read), points the program-running keys its verbs can reach at nothing and switches each of
     those stores off. It is an ENVIRONMENT and not a list of `-c` flags on purpose: the read-only pins
     (tests/unit/_readonly_pins.py) find a git verb by reading a static argv literal, so every call here stays
     `subprocess.run(["git", "<verb>", …], **git_kwargs(repo))` and a new pin (`assert_git_runs_in_the_seam`) holds
     each one to it.

  3. WHAT GIT RETURNS IS BYTES UNTIL IT IS SHOWN. A ref name, an author, a subject and a path are whatever somebody
     committed; none of it has to be text. Nothing here decodes on git's behalf — `show` turns bytes into the text
     a person is shown, and says whether that text is the bytes exactly or an escaped spelling of them.

  4. A COMMIT'S TREE IS READ OBJECT BY OBJECT, AND EVERY OBJECT IS RE-HASHED (`read_tree_verified`). Git does not
     check that an object it reads is the object its name says, and an export (`git archive`) is the commit as its
     own attribute files, the repository's filters and its replacement refs make it. A gate that judges a commit
     must judge THAT commit: the tree its bytes name, the files those trees name, regular files only.

  5. ROOT DOES NOT RUN GIT IN A REPOSITORY THE SERVICE CAN WRITE. The environment of rule 2 answers the ways a
     repository makes git run a program or read another store THAT SOMEBODY HAS LISTED. What nobody has listed —
     the parser of a pack, of a ref file, of a config line, and whatever else those files can make a git do — is
     reached by whoever git runs as. So a caller with effective uid 0 runs every git of the seam as the SERVICE
     uid (`SERVICE_UID`:`SERVICE_GID`, no supplementary group, in a session of its own, started in `/`) whenever
     anything in the repository is the service's to write (`service_writable`): whatever a writer of the
     canonical can then make that git do, it does as the service's uid and not as root. A repository the service
     can write nothing of — root's own — is one root may run git in.

Needs git 2.31 or later: the config overrides travel in `GIT_CONFIG_COUNT`, which an older git ignores without a
word (every hook would run). `GIT_NO_LAZY_FETCH` is honoured by git 2.45 and later; before it `protocol.allow=never`
is what stops a promisor remote.

Nothing here writes the CANONICAL (the module is in the pinned layer, so the completeness sweep would name a verb
that did). `export_commit` writes a commit's files into a directory the caller names; it is a registered write
verb for that reason, so no read view can reach it.
"""
import errno
import hashlib
import os
import re
import stat
import subprocess
import threading

MAIN = "refs/heads/main"
PROPOSED = "refs/heads/proposed/"

# The uid and gid the network service runs as (`user: "1001:1001"` in docker/services/) and the canonical's group
# grant names (`local-canonical.yml`: `<operator>:1001`, `2775`, `core.sharedRepository=group`). Numbers, not
# names: the canonical is reached from containers that have no user database in common.
SERVICE_UID = 1001
SERVICE_GID = 1001

# The ONLY two run-id shapes the service mints: `uuid4().hex[:12]` (the GUI) is twelve lower-case hex digits;
# `api.audit.mint_run_id` is a UTC stamp `YYYYMMDDTHHMMSSZ`, a hyphen and six hex digits. Matched with fullmatch —
# `^…$` with `re.match` lets `<id>\n` through (Python's `$` matches before a final newline). The promote play's
# assert spells the same rule in Jinja; one test (test_canon.py) feeds both the same accepted and refused samples.
_RUN_ID = re.compile(r"[0-9a-f]{12}|[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}")
_OID = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")          # a sha1 (40 hex) or sha256 (64 hex) object name


class Unreadable(Exception):
    """The canonical could not be read: git is absent, did not answer in time, or refused the repository. The
    message is a sentence of ours — never git's output and never a name read from the repository."""


class TreeRefused(Unreadable):
    """A commit's tree is not one the reader takes: an object is missing or is not what its name says, an entry is
    a link or a submodule, a name no checkout could hold, more than the caps. The fault is in what was COMMITTED
    (or in the object store), not in our ability to run — a caller that judges the commit refuses it; a caller
    told only `Unreadable` could not tell that from "git is not installed"."""


def valid_run_id(s):
    """True iff `s` is a run id of one of the two minted shapes — the only strings that ever become a
    `refs/heads/proposed/<id>` name. A git option, a path, a branch name, a trailing newline are all refused."""
    return isinstance(s, str) and bool(_RUN_ID.fullmatch(s))


def valid_oid(s):
    """True iff `s` is a full object name that names something: forty or sixty-four lower-case hex digits and not
    all zeros (to `update-ref` the null id as an old value means "must not exist", and as a new value "delete")."""
    return isinstance(s, str) and bool(_OID.fullmatch(s)) and set(s) != {"0"}


def proposal_ref(run_id):
    """The full name of the proposal a run id names. Raises ValueError for anything but a minted id, so a caller
    cannot build a ref name out of a string it did not check."""
    if not valid_run_id(run_id):
        raise ValueError("not a run id")
    return PROPOSED + run_id


# What a git of the canonical inherits from the caller: where programs and temporary files are, and the locale.
# No HOME (a per-user config, and on Windows the home of an identity), nothing GIT_*, no SOPS_*, no pager.
_KEEP = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG", "LC_ALL")


def git_env(repo):
    """The only environment a git that touches the canonical runs in. Every entry answers something of the
    repository that would otherwise decide what git does (tests/unit/test_canon.py plants the thing an entry
    answers wherever a verb used here can show it, and holds the whole list by name):

      GIT_DIR, GIT_COMMON_DIR          the repository is named, not discovered, and its config and objects are its
                                       own even beside a planted `commondir` file (its refs are not — `git_kwargs`
                                       refuses such a repository)
                                       (with GIT_COMMON_DIR set git also ignores the repository's `core.bare` and
                                       `core.worktree` and takes the CALLER'S working directory as the work tree:
                                       no verb here may look at a work tree or an index — see `--ignore-submodules`
                                       in service/pending.py for the one that did)
      GIT_CONFIG_NOSYSTEM / _GLOBAL    no config scope but the repository's own and the keys below
      core.hooksPath                   no hook runs (`update-ref` runs `reference-transaction`)
      core.fsmonitor, core.pager, core.attributesFile, log.showSignature, protocol.allow
                                       the other keys through which a config can name a program, or a place to
                                       read one, FOR THE VERBS USED HERE — a new verb needs its own look (`git
                                       archive`, a patch `diff` and `log` each reach keys this list does not hold)
      GIT_NO_REPLACE_OBJECTS           a `refs/replace/*` does not stand in for the object asked for
      GIT_GRAFT_FILE, GIT_SHALLOW_FILE `info/grafts` and `shallow` do not rewrite a commit's parents
      core.commitGraph=false           parents and trees come from the commits, not from `objects/info/commit-graph`
      gc.auto, maintenance.auto, GIT_NO_LAZY_FETCH, GIT_OPTIONAL_LOCKS, GIT_TERMINAL_PROMPT
                                       a read starts nothing, fetches nothing, locks nothing, asks nothing
      core.packedRefsTimeout, core.filesRefLockTimeout
                                       a write waits for a lock for less time than its caller waits for it, so git
                                       gives up by itself and removes its own lock files (a killed git leaves them)
      GIT_COMMITTER_NAME / _EMAIL      a reflog line, where the repository keeps one, carries this identity and not
                                       one the repository's `user.name` chose
      safe.directory                   the named repository is the one meant, whoever owns it

    Not answered here: the object store itself (an alternates file, an object swapped under its name) and the ref
    files (`packed-refs`, a reftable) — what they hold is what git reads."""
    cfg = (("core.hooksPath", os.devnull), ("core.fsmonitor", "false"), ("core.pager", "cat"),
           ("core.attributesFile", os.devnull), ("core.commitGraph", "false"), ("log.showSignature", "false"),
           ("gc.auto", "0"), ("maintenance.auto", "false"), ("protocol.allow", "never"),
           ("advice.graftFileDeprecated", "false"), ("core.packedRefsTimeout", "1000"),
           ("core.filesRefLockTimeout", "100"), ("safe.directory", repo))
    env = {k: os.environ[k] for k in _KEEP if k in os.environ}
    env.update(GIT_DIR=repo, GIT_COMMON_DIR=repo, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_NO_REPLACE_OBJECTS="1", GIT_GRAFT_FILE=os.devnull, GIT_SHALLOW_FILE=os.devnull,
               GIT_ATTR_NOSYSTEM="1", GIT_NO_LAZY_FETCH="1", GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0",
               GIT_PAGER="cat", GIT_LITERAL_PATHSPECS="1",
               GIT_COMMITTER_NAME="kontroll", GIT_COMMITTER_EMAIL="kontroll@example.invalid",
               GIT_CONFIG_COUNT=str(len(cfg)))
    for i, (k, v) in enumerate(cfg):
        env["GIT_CONFIG_KEY_%d" % i], env["GIT_CONFIG_VALUE_%d" % i] = k, v
    return env


# The parts of a bare repository through which git reads or writes SOMEWHERE ELSE when one of them is a symbolic
# link. No repository this tool creates has one.
_NOT_A_LINK = ("refs", "packed-refs", "logs", "objects", "HEAD", "config")


def git_dir(path):
    """The git DIRECTORY of the repository at `path`: the path itself when it IS one (a bare repository — the
    canonical), else `<path>/.git` for a clone. The seam names a repository to git outright, so every caller that
    is handed "a repository" by a person or a play turns it into the directory once, here, and nothing is
    discovered from a working directory.

    The path itself is asked FIRST. A directory called `.git` inside the canonical is something a writer of the
    canonical can make; taken for the repository, it had every promote read, judge and move the refs of a
    repository that writer built, and report success while the canonical's `main` stood still (the same redirection
    a `commondir` file is refused for in `git_kwargs`)."""
    if all(os.path.lexists(os.path.join(path, part)) for part in ("HEAD", "objects", "refs")):
        return path
    dot = os.path.join(path, ".git")
    return dot if os.path.isdir(dot) else path


def git_kwargs(repo, fed=False):
    """The keyword arguments of every `subprocess` call that runs git on the canonical: `**git_kwargs(repo)`. One
    function, so that what such a process runs in — and whether it runs at all — is decided in one place. A call
    that FEEDS git (an `input=`, a `stdin=subprocess.PIPE` of its own) says so: `git_kwargs(repo, fed=True)`.

    It REFUSES (Unreadable) a repository that holds a `commondir` file. That file hands the repository's config,
    hooks, objects and refs to the directory it names; `GIT_COMMON_DIR` takes the first three back, but git's ref
    store reads the file whatever the environment says (measured: the refs then come from the planted directory
    and the objects from this one). It also refuses one whose `refs`, `packed-refs`, `logs`, `objects`, `HEAD` or
    `config` is a symbolic link: git reads refs through the first two as it does through `commondir`, and a delete
    rewrites `packed-refs` and removes a file under `logs` *through* the link. No canonical of ours has either, so
    one that does is not read and not written. This is a check before the process starts, not a lock: it catches a
    planted file, not a race.

    And it decides AS WHOM (rule 5 of the module): for a caller with effective uid 0, in a repository of which
    anything is the service's to write, the keywords carry `user`, `group` and an empty `extra_groups` — the
    child is the service uid before git starts. A root that cannot change identity is refused (Unreadable)
    rather than left to run git as itself. The identity comes with a SESSION of the child's own
    (`start_new_session`) and, unless the caller feeds git, a standard input that is nothing: the service's git
    is given the service's uid and nothing of the caller's session — not the terminal the caller sits at — and
    with a working directory of `/`: the caller's own may be one the service cannot enter."""
    if os.path.lexists(os.path.join(repo, "commondir")):
        raise Unreadable("the canonical holds a `commondir` file, which would have its refs read from another "
                         "directory; nothing was read")
    for part in _NOT_A_LINK:
        if os.path.islink(os.path.join(repo, part)):
            raise Unreadable("the canonical's `%s` is a symbolic link, so git would read or write somewhere "
                             "else; nothing was read" % part)
    kw = {"env": git_env(repo)}
    who = identity_kwargs(repo)
    if who:
        kw.update(who)
        if not fed:
            kw["stdin"] = subprocess.DEVNULL
        # And it starts in `/`, with the repository named by its full path. The child would otherwise keep the
        # CALLER'S working directory while giving up the caller's identity: git takes that directory for a work
        # tree (GIT_COMMON_DIR is set), `rev-list <id>` asks whether `<id>` is also a file there, and in a
        # directory the service uid may not search — the operator's home, root's — that question is refused.
        kw.update(env=git_env(os.path.abspath(repo)), cwd=os.sep)
    return kw


def identity_kwargs(repo):
    """AS WHOM a process that reads what the repository at `repo` holds is started (rule 5): `{}` — as the
    caller — or, for a caller with effective uid 0 where anything in the repository is the service's to write,
    `user`, `group`, an empty `extra_groups` and a session of the child's own. `subprocess` applies them in the
    child, between fork and exec: setgroups([]), setgid, setuid — in that order, so nothing of root's is left
    when the program starts. Raises `Unreadable` for a root that cannot change identity. It is `git_kwargs`' own
    answer, by itself, for the one caller that hands git a FILE of the repository and not the repository (the
    audit's `git config --file -`); that caller sees to the standard input and the working directory itself."""
    if hasattr(os, "geteuid") and os.geteuid() == 0 and service_writable(repo):
        if not _can_drop():
            raise Unreadable("could not drop to the service uid; nothing was read or changed")
        return {"user": SERVICE_UID, "group": SERVICE_GID, "extra_groups": [], "start_new_session": True}
    return {}


def _theirs(st):
    """Is the thing this `stat` describes the service's to write? Its own; or its group's, with the group's
    write bit; or anybody's."""
    return (st.st_uid == SERVICE_UID or (st.st_gid == SERVICE_GID and bool(st.st_mode & 0o020))
            or bool(st.st_mode & 0o002))


def _listed(path, st):
    """Does an access-control list stand on `path` that could give the service write where the mode names
    another group? A list's grants are capped by its mask, and the mask is what the GROUP's bits of the mode
    show: without the group's write bit no entry of a list writes anything. With it, a list on the entry is
    taken for a grant — it is not parsed. A file system that keeps no such lists has none; a list that cannot
    be asked for is one."""
    if not st.st_mode & 0o020 or not hasattr(os, "listxattr"):
        return False
    try:
        return "system.posix_acl_access" in os.listxattr(path, follow_symlinks=False)
    except OSError as e:
        return e.errno not in (errno.ENOTSUP, errno.ENODATA)


def service_writable(repo):
    """True unless the service uid can write NOTHING of the repository at `repo`, as far as owner, group and
    mode say: its top directory and every file, directory and link below it belong to somebody else, with no
    write bit for the service's group or for everybody, and none of them is a symbolic link. (An access-control
    list is not parsed: on an entry that shows a group write bit, one that is there is taken for a grant —
    `_listed`.) The play's layout answers True at the first look (the top directory is the service group's to
    write); a repository root made for itself answers False.

    The WHOLE repository is asked, not one directory of it, because one directory is something the service can
    exchange: with write on `refs/` it renames `refs/heads` away and puts a LINK to a root-owned directory in
    its place, or — where one stands beside it — a directory of somebody else's that has no group write bit: a
    single `stat` of `refs/heads` then said "not the service's", and root went on running git in it (measured). What
    the service cannot exchange is the top directory itself (that takes write on its PARENT), and nothing at all
    in a repository of which nothing is its own.

    Fails toward True: a part that cannot be looked at, or that goes away while it is looked at, is a part
    somebody is writing. A path that is not there, or is not a directory, answers False — there is no
    repository for a git to run in, as anybody."""
    try:
        top = os.stat(repo)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    if not stat.S_ISDIR(top.st_mode):
        return False
    if _theirs(top) or _listed(repo, top):
        return True
    pending = [repo]
    while pending:
        here = pending.pop()
        try:
            with os.scandir(here) as entries:
                for entry in entries:
                    st = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(st.st_mode) or _theirs(st) or _listed(entry.path, st):
                        return True
                    if stat.S_ISDIR(st.st_mode):
                        pending.append(entry.path)
        except OSError:
            return True
    return False


_DROP = []          # the answer of `_can_drop`, once asked: a process's capabilities do not change under it


def _can_drop():
    """Can this process — effective uid 0 — start a child as the service uid? Read once, from what the kernel
    says of the process (Linux): its effective capability set must hold CAP_SETGID and CAP_SETUID, and its user
    namespace must not deny `setgroups`. A root without them (a container started with the capability dropped,
    a user namespace that denies `setgroups`) would have every git of the seam fail at the change of identity,
    before git starts;
    this is asked first so that the caller is told THAT, in one sentence, instead of "git could not be run" at
    whichever call came first. It decides a sentence and nothing else: where the kernel does not say (no
    `/proc`), or says yes and the change is refused all the same, the child still never becomes git as root —
    `subprocess` changes identity before it executes anything, and a failed change is a failed start."""
    if not _DROP:
        able = True
        try:
            with open("/proc/self/status", encoding="ascii", errors="replace") as fh:
                for line in fh:
                    if line.startswith("CapEff:"):
                        held = int(line.split()[1], 16)
                        able = bool(held >> 6 & 1) and bool(held >> 7 & 1)      # CAP_SETGID is 6, CAP_SETUID is 7
        except (OSError, ValueError, IndexError):
            pass
        try:
            with open("/proc/self/setgroups", encoding="ascii", errors="replace") as fh:
                able = able and fh.read().strip() != "deny"
        except OSError:
            pass
        _DROP.append(able)
    return _DROP[0]


_REF_FMT = "%(refname)%00%(objectname)%00%(objecttype)%00%(symref)"
_PROPOSAL_FMT = (_REF_FMT + "%00%(committerdate:unix)%00%(committerdate:iso8601-strict)%00%(authorname)"
                 "%00%(contents:subject)")


def _for_each_ref(repo, fmt, pattern, fields, timeout):
    """`(records, left_out)`: the records `git for-each-ref` prints for `pattern`, each a list of `fields` byte
    strings (the last field keeps any NUL of its own), and whether git said — on stderr, which is otherwise empty
    here — that it left a ref out (a file that holds no object name, a file whose name it does not accept). Raises
    Unreadable when git cannot run, is too slow, or will not read `repo`; one ref whose OBJECT git cannot read is
    such a refusal, for the whole listing."""
    try:
        p = subprocess.run(["git", "for-each-ref", "--format=" + fmt, pattern],
                           capture_output=True, timeout=timeout, **git_kwargs(repo))
    except (OSError, subprocess.SubprocessError):
        raise Unreadable("git could not be run on the canonical") from None
    if p.returncode != 0:
        raise Unreadable("git could not read the canonical")
    out = []
    for line in p.stdout.split(b"\n"):
        rec = line.split(b"\0", fields - 1)
        if len(rec) == fields:                       # an empty or cut line is not a record
            out.append(rec)
    return out, bool(p.stderr)


def _text(b):
    """A field git wrote in ASCII (an object name, a type, a date) as str; "" for anything else."""
    try:
        return b.decode("ascii")
    except UnicodeDecodeError:
        return ""


def read_ref(repo, name, timeout=5):
    """The ref `name` (a FULL name) as `(oid, objecttype, symref)`, or None when the canonical has no such ref.
    `oid` is the ref's own value — a tag object's id for a ref that names one, never the commit it peels to — and
    `symref` is "" for an ordinary ref, else the name a symbolic ref points at (`oid` is then that ref's value: a
    caller that wants a branch refuses it). Raises Unreadable when the repository cannot be read, and when git
    prints the name more than once (a ref store written by hand).

    WHY for-each-ref and not rev-parse: `rev-parse` LOOKS A NAME UP (a tag `main`, a `refs/main`, a file `main` in
    the git directory each win over the branch for the short name) and follows a symbolic ref without saying so;
    `for-each-ref` prints the refs that exist under the names they have. The pattern is a prefix match, so the one
    record whose name is `name` exactly is kept."""
    want = name.encode("utf-8")
    records, _ = _for_each_ref(repo, _REF_FMT, name, 4, timeout)
    hits = [(oid, objtype, symref) for refname, oid, objtype, symref in records
            if refname == want and valid_oid(_text(oid))]
    if len(hits) > 1:
        raise Unreadable("the canonical lists one ref name more than once")
    if not hits:
        return None
    oid, objtype, symref = hits[0]
    return _text(oid), _text(objtype), symref.decode("utf-8", "backslashreplace")


def proposals(repo, timeout=5):
    """Everything under `refs/heads/proposed/`, as `(found, apart, left_out)`:

      found     [{"run_id", "oid", "when": int, "date", "author": bytes, "subject": bytes}] — a ref is a proposal
                iff its name is the prefix plus a minted run id, its value is a commit, it is not a symbolic ref,
                and a second read of that one name gives the same commit;
      apart     [{"ref": bytes, "oid", "why"}] — every other ref under the prefix (a name that is not a run id, a
                symbolic ref, a ref that names a tag or a blob, a name git gives more than one value). They are
                returned, not dropped: a listing that hid them would hide exactly what a direct writer of the
                repository leaves behind;
      left_out  True when git said it skipped a ref it could not read — the listing is then not all there is.

    Raises Unreadable when the repository cannot be read. Author and subject stay bytes (see `show`); `when` is the
    committer time in seconds (0 when the commit carries none git can read)."""
    found, apart = [], []
    prefix = PROPOSED.encode("ascii")
    records, left_out = _for_each_ref(repo, _PROPOSAL_FMT, PROPOSED, 8, timeout)
    for refname, oid, objtype, symref, when, date, author, subject in records:
        oid = _text(oid)
        run_id = _text(refname[len(prefix):]) if refname.startswith(prefix) else ""
        if not valid_run_id(run_id):
            why = "its name is not a run id"
        elif symref:
            why = "it is a symbolic ref"
        elif objtype != b"commit" or not valid_oid(oid):
            why = "it does not name a commit"
        else:
            found.append({"run_id": run_id, "oid": oid, "when": int(when) if when.isdigit() else 0,
                          "date": _text(date), "author": author, "subject": subject})
            continue
        apart.append({"ref": refname, "oid": oid if valid_oid(oid) else "", "why": why})
    # The listing is a walk of the ref store; a promote and a discard look ONE name up. A hand-written `packed-refs`
    # can make the two disagree (a name listed twice, or out of the order its header claims), so each proposal is
    # read once more by its exact name and is one only if both reads give the same commit.
    # (A second read that cannot be MADE is not a disagreement: `Unreadable` goes to the caller, as for the walk.)
    kept = []
    for f in found:
        exact = read_ref(repo, PROPOSED + f["run_id"], timeout)
        if exact == (f["oid"], "commit", "") and sum(1 for g in found if g["run_id"] == f["run_id"]) == 1:
            kept.append(f)
        else:
            apart.append({"ref": (PROPOSED + f["run_id"]).encode("ascii"), "oid": f["oid"],
                          "why": "git gives its name more than one value"})
    return kept, apart, left_out


def is_ancestor(repo, ancestor, descendant, timeout=5):
    """Is the commit `ancestor` reachable from the commit `descendant` — the fast-forward question, asked of two
    IDS (never of names: by now each ref has been read once). True, False, or None when it cannot be told (not
    ids, git cannot run, the repository is refused, an id that is not a commit of it): a caller treats None as
    "unknown", never as yes."""
    if not (valid_oid(ancestor) and valid_oid(descendant)):
        return None
    try:
        rc = subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, descendant],
                            capture_output=True, timeout=timeout, **git_kwargs(repo)).returncode
    except (OSError, subprocess.SubprocessError, Unreadable):
        return None
    return True if rc == 0 else False if rc == 1 else None


def commits_between(repo, main_id, oid, limit=20, timeout=10):
    """What a fast-forward of `main_id` to `oid` would land, for a person to read before it happens:
    `(count, [(commit id, subject bytes), …])` — the number of commits reachable from `oid` and not from `main_id`,
    and the newest `limit` of them. Both are ids; subjects stay bytes (see `show`). Raises `Unreadable` when git
    cannot say. `rev-list` prints ids and subjects and starts no program for either.

    THE LISTING IS ONE RECORD PER COMMIT, AND EACH RECORD NAMES ITS COMMIT TWICE. git prints `commit <id>` and
    then the format's line — but NO line at all when the format expands to nothing, so with a bare `%s` a commit
    whose message is empty shifted every pair after it, and a neighbour whose subject was the text `commit <40
    hex>` was then read as a commit of that id. The format is `%H%x00%s`: never empty, and the id in it must be
    the id of the header above it. `--encoding=none` because the subject is otherwise RE-ENCODED to what the
    repository's `i18n.logOutputEncoding` / `i18n.commitEncoding` names — to UTF-16, a subject of chosen
    characters comes out as bytes that hold a newline and a second `commit <id>` line. And the listing must be
    as long as the count says and open with `oid` itself: the commit that lands is the first line of the preview."""
    if not (valid_oid(main_id) and valid_oid(oid)):
        raise Unreadable("the commits to compare are not named by full ids")
    try:
        n = subprocess.run(["git", "rev-list", "--count", oid, "^" + main_id],
                           capture_output=True, timeout=timeout, **git_kwargs(repo))
        p = subprocess.run(["git", "rev-list", "--max-count=%d" % limit, "--encoding=none", "--format=%H%x00%s",
                            oid, "^" + main_id], capture_output=True, timeout=timeout, **git_kwargs(repo))
    except (OSError, subprocess.SubprocessError):
        raise Unreadable("git could not be run on the canonical") from None
    count = _text(n.stdout.strip())
    if n.returncode != 0 or p.returncode != 0 or not count.isdigit():
        raise Unreadable("git could not list the commits between the two ids")
    odd = Unreadable("git's listing of the commits is not in the form that was asked for")
    lines, listed = p.stdout.split(b"\n"), []
    if lines.pop() != b"" or len(lines) % 2:
        raise odd
    for i in range(0, len(lines), 2):                     # `commit <id>`, then `<id>` NUL `<subject>`
        head, (named, nul, subject) = _text(lines[i]), lines[i + 1].partition(b"\0")
        if not (head.startswith("commit ") and valid_oid(head[7:]) and nul and _text(named) == head[7:]):
            raise odd
        listed.append((head[7:], subject))
    if len(listed) != min(int(count), max(limit, 0)) or (listed and listed[0][0] != oid):
        raise odd
    return int(count), listed


def show(b):
    """Bytes git returned, as `(text, exact)` for a person to read. `exact` is True when `b` is UTF-8 and every
    character of it is printable: the text IS the bytes. Otherwise the text is an escaped spelling — a byte that
    is not UTF-8 as `\\xNN`, a character that does not print (a newline, an escape, a direction override, a no-break
    space) as its `\\n` / `\\xNN` / `\\uNNNN` escape — `\\xNN` above `\\x7f` is always a byte, never a character,
    which is spelled `\\u00NN` — and a backslash doubled so the spelling reads one way only — and `exact` is False,
    so the caller can say the name is not shown as written. `exact` is about the bytes, not the look: a printable
    character can be invisible or look like another, and the caller is not told. Never raises."""
    try:
        s = b.decode("utf-8")
        if s.isprintable():
            return s, True
    except UnicodeDecodeError:
        pass
    out = []
    for ch in b.decode("utf-8", "surrogateescape"):
        o = ord(ch)
        if 0xDC80 <= o <= 0xDCFF:
            out.append("\\x%02x" % (o - 0xDC00))             # a byte that was not UTF-8
        elif ch == "\\":
            out.append("\\\\")
        elif ch.isprintable():
            out.append(ch)
        elif 0x80 <= o <= 0xFF:
            out.append("\\u%04x" % o)                         # NOT `\xNN`: that spelling is a byte that was not UTF-8
        else:
            out.append(ch.encode("unicode_escape").decode("ascii"))
    return "".join(out), False


# ---- one commit's tree, as it is ------------------------------------------------------------------------------ #
_FILE_MODES = (b"100644", b"100755")
_TREE_MODE = b"40000"
# Far above any tree this tool ships or stages (the shipped tree is a few hundred files, a few megabytes): the caps
# exist so that a commit somebody else wrote cannot make the reader hold an unbounded amount, or run without end.
MAX_TREE_ENTRIES = 20000
MAX_TREE_BYTES = 64 * 1024 * 1024
TREE_READ_SECONDS = 120


_NAME_BYTES = 255                 # the longest name ext4, xfs, btrfs and NTFS take; git cannot check a longer one out
_PATH_BYTES = 4000                # under PATH_MAX with room for the directory the files are written into
# The characters HFS+ ignores in a name: with one of them inside it, `.git` is still `.git` there (git: core.protectHFS)
_HFS_IGNORED = dict.fromkeys(list(range(0x200C, 0x2010)) + list(range(0x202A, 0x202F)) + list(range(0x206A, 0x2070))
                             + [0xFEFF])


def _is_dot_git(name):
    """Is `name` a spelling of `.git` that git's own checkout refuses? `.git` in any case; and — on EVERY platform,
    because `core.protectNTFS` is on by default — `.git` or its short name `git~1` followed by any run of dots and
    spaces and by an optional `:stream` (NTFS drops all of that and opens `.git`); and `.git` with characters HFS+
    ignores inside it. A commit with one of these on `main` is a `main` no clone can check out."""
    low = name.lower()
    if low.split(b":", 1)[0].rstrip(b". ") in (b".git", b"git~1"):
        return True
    try:
        return low.decode("utf-8").translate(_HFS_IGNORED) == ".git"
    except UnicodeDecodeError:
        return False


def _refused_name(name):
    """A tree entry's name the reader refuses: empty, `.`, `..`, one that holds a path separator or a control
    byte, one longer than a file system takes, or a spelling of `.git` (`_is_dot_git`). Git's own checkout takes
    none of them on the node; a tree that holds one was written by hand."""
    return (not name or name in (b".", b"..") or b"/" in name or b"\\" in name or len(name) > _NAME_BYTES
            or any(c < 0x20 or c == 0x7f for c in name) or _is_dot_git(name))


def read_tree_verified(repo, commit_id, max_entries=MAX_TREE_ENTRIES, max_bytes=MAX_TREE_BYTES,
                       timeout=TREE_READ_SECONDS):
    """Every regular file of the commit `commit_id`, as `{path bytes: content bytes}` — or `TreeRefused(<the rule>)`
    (and plain `Unreadable` when git itself cannot be run or stops answering).

    ONE `git cat-file --batch` process answers for every object, and nothing it answers is believed: an object's
    header must name the id that was asked for and the kind its place requires, and its bytes must HASH to that id
    — the commit, every tree, every blob. Trees are parsed from their own bytes (`<mode> <name>\\0<raw id>`), and
    the root tree's id is taken from the commit's first line. So nothing in the repository can make this return a
    tree other than the one `commit_id` names — not an attribute or a filter (no path is ever checked out or
    exported), not a replace ref, a graft or a commit-graph (the seam, and the commit's own bytes), not an object
    file swapped under its name (the hash). The worst any of them can do is make it refuse.

    Refused, each with a sentence that names the RULE and never a path or a value: an object that is missing, of
    another kind, or not what its name says; an entry that is not a regular file or a directory (a symbolic link,
    a submodule); a name no checkout could take, or one name twice in a tree; more entries or more bytes than the
    caps; git not answering within `timeout` seconds."""
    if not valid_oid(commit_id):
        raise TreeRefused("the commit to read is not named by a full id")
    digest = hashlib.sha1 if len(commit_id) == 40 else hashlib.sha256
    raw_len = len(commit_id) // 2
    try:
        proc = subprocess.Popen(["git", "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, **git_kwargs(repo, fed=True))
    except OSError:
        raise Unreadable("git could not be run on the canonical") from None
    timed_out = threading.Event()

    def end():                                                 # a git that stops answering is ended, not waited for
        timed_out.set()
        proc.kill()
    watchdog = threading.Timer(timeout, end)
    watchdog.daemon = True
    watchdog.start()
    left = {"entries": max_entries, "bytes": max_bytes, "answered": 0}

    def gone():
        """git's answer stopped short. WHO ended it says whose fault that is: the watchdog (git was too slow — ours);
        git itself BEFORE it answered for anything (it cannot open the repository — ours); git itself after it had
        answered for the commit (it died on one object: a file of the store it cannot inflate, or of a kind it does
        not know — the store's fault, like an object that is missing)."""
        if timed_out.is_set():
            return Unreadable("git did not answer for an object within the time the reader gives it")
        if left["answered"]:
            return TreeRefused("git ended on an object the commit names: the object store holds one git cannot read")
        return Unreadable("git gave no answer for an object (it ended, or it cannot read the repository)")

    def exactly(n):
        chunks = []
        while n:
            chunk = proc.stdout.read(n)
            if not chunk:
                raise gone()
            chunks.append(chunk)
            n -= len(chunk)
        return b"".join(chunks)

    def obj(oid, kind):
        try:
            proc.stdin.write(oid.encode("ascii") + b"\n")
            proc.stdin.flush()
        except OSError:
            raise gone() from None
        header = proc.stdout.readline()
        if not header.endswith(b"\n"):                     # nothing, or a line cut short: git ended, or was ended
            raise gone()
        parts = header[:-1].split(b" ")
        if parts == [oid.encode("ascii"), b"missing"]:
            raise TreeRefused("an object the commit names is missing")
        if len(parts) != 3 or parts[0] != oid.encode("ascii") or not parts[2].isdigit():
            raise TreeRefused("git's answer for an object is not in the form that was asked for")
        if parts[1] != kind:
            raise TreeRefused("an object is not the kind its place in the tree asks for")
        left["bytes"] -= int(parts[2])
        if left["bytes"] < 0:
            raise TreeRefused("the tree is larger than the gate reads")
        body = exactly(int(parts[2]))
        if exactly(1) != b"\n":
            raise TreeRefused("git's answer for an object is not in the form that was asked for")
        if digest(kind + b" " + parts[2] + b"\0" + body).hexdigest() != oid:
            raise TreeRefused("an object is not what its name says")
        left["answered"] += 1
        return body

    try:
        first = obj(commit_id, b"commit").split(b"\n", 1)[0]
        root = _text(first[5:])
        if not first.startswith(b"tree ") or len(root) != len(commit_id) or not valid_oid(root):
            raise TreeRefused("the commit does not name a tree")
        files, pending = {}, [(b"", root)]
        while pending:
            prefix, tree_id = pending.pop()
            data, at, names = obj(tree_id, b"tree"), 0, set()
            while at < len(data):
                sp = data.find(b" ", at)
                nul = data.find(b"\0", sp + 1) if sp != -1 else -1
                if sp == -1 or nul == -1 or nul + 1 + raw_len > len(data):
                    raise TreeRefused("a tree object is not in the form git writes")
                mode, name, oid = data[at:sp], data[sp + 1:nul], data[nul + 1:nul + 1 + raw_len].hex()
                at = nul + 1 + raw_len
                left["entries"] -= 1
                if left["entries"] < 0:
                    raise TreeRefused("the tree holds more entries than the gate reads")
                if _refused_name(name) or name in names or len(prefix) + len(name) > _PATH_BYTES:
                    raise TreeRefused("the tree holds an entry whose name no checkout could take")
                names.add(name)
                if mode == _TREE_MODE:
                    pending.append((prefix + name + b"/", oid))
                elif mode in _FILE_MODES:
                    files[prefix + name] = obj(oid, b"blob")
                else:
                    raise TreeRefused("the tree holds an entry that is not a regular file or a directory "
                                      "(a symbolic link, a submodule, or a mode git does not write)")
        return files
    finally:
        watchdog.cancel()
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except OSError:
                pass
        proc.kill()
        proc.wait()


def write_files(files, dest, prefixes=None):
    """Write `files` (`{path bytes: content bytes}`, as `read_tree_verified` returns them) under the directory
    `dest` — all of them, or those under one of `prefixes` (bytes such as `b"modules/"`; a prefix without the slash
    names that one file). Returns the number written.

    Each file is created exclusively (`xb`): `dest` is expected to be a fresh directory, and a path that is
    already there — two names of the tree that one filesystem folds into one — is refused, never overwritten.
    (Files only: two DIRECTORY names one file system folds into one — `Modules/` and `modules/` where case is
    folded — are written into one directory. No file system of a node folds; a promote run on one that does can
    judge another tree than the node will hold.) A path that is not UTF-8 is refused (`TreeRefused`: the tree's
    fault), and so is one that would be written outside `dest`. A file that cannot be written is `Unreadable`
    (ours). What was already written stays for the caller to remove; the callers hand in a temporary directory."""
    written = 0
    for path in sorted(files):
        if prefixes is not None and not any(path.startswith(p) if p.endswith(b"/") else path == p for p in prefixes):
            continue
        try:
            rel = path.decode("utf-8")
        except UnicodeDecodeError:
            raise TreeRefused("the tree holds a path that is not UTF-8") from None
        parts = rel.split("/")
        target = os.path.join(dest, *parts)
        # The reader hands in no such path; another caller might, and on Windows a name like `D:x` is a path on
        # another drive: nothing is written that is not under `dest`.
        if (any(p in ("", ".", "..") for p in parts)
                or not os.path.abspath(target).startswith(os.path.abspath(dest).rstrip(os.sep) + os.sep)):
            raise TreeRefused("the tree holds a path that would be written outside the directory it was to go into")
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "xb") as fh:
                fh.write(files[path])
        except FileExistsError:
            raise TreeRefused("the tree holds two paths that this filesystem takes for one") from None
        except OSError:
            raise Unreadable("a file of the tree could not be written where it was to be read") from None
        written += 1
    return written


def export_commit(repo, commit_id, dest, prefixes=None):
    """Write the regular files of the commit `commit_id` under the directory `dest` (see `write_files`). The files
    are what `read_tree_verified` returns, so this is the commit as it is: no attribute hides or rewrites a path,
    no filter runs, and no link is created (a tree that holds one is refused). Returns the number written."""
    return write_files(read_tree_verified(repo, commit_id), dest, prefixes)
