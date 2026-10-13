"""canon — how the tool reads the canonical bare repository, and the one environment a git that touches it runs in (C10).

The canonical (`/srv/kontroll.git`) is the instance's source of truth: `refs/heads/main` is what runs, and each
`refs/heads/proposed/<run id>` is a change the network service staged for a human to promote. Three rules live here,
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

Needs git 2.31 or later: the config overrides travel in `GIT_CONFIG_COUNT`, which an older git ignores without a
word (every hook would run). `GIT_NO_LAZY_FETCH` is honoured by git 2.45 and later; before it `protocol.allow=never`
is what stops a promisor remote.

No write verb lives in this module (it is in the pinned layer, so the completeness sweep would name one).
"""
import os
import re
import subprocess

MAIN = "refs/heads/main"
PROPOSED = "refs/heads/proposed/"

# The ONLY two run-id shapes the service mints: `uuid4().hex[:12]` (the GUI) is twelve lower-case hex digits;
# `api.audit.mint_run_id` is a UTC stamp `YYYYMMDDTHHMMSSZ`, a hyphen and six hex digits. Matched with fullmatch —
# `^…$` with `re.match` lets `<id>\n` through (Python's `$` matches before a final newline). The promote play's
# assert spells the same rule in Jinja; one test (test_canon.py) feeds both the same accepted and refused samples.
_RUN_ID = re.compile(r"[0-9a-f]{12}|[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}")
_OID = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")          # a sha1 (40 hex) or sha256 (64 hex) object name


class Unreadable(Exception):
    """The canonical could not be read: git is absent, did not answer in time, or refused the repository. The
    message is a sentence of ours — never git's output and never a name read from the repository."""


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


def git_kwargs(repo):
    """The keyword arguments of every `subprocess` call that runs git on the canonical: `**git_kwargs(repo)`. One
    function, so that what such a process runs in — and whether it runs at all — is decided in one place.

    It REFUSES (Unreadable) a repository that holds a `commondir` file. That file hands the repository's config,
    hooks, objects and refs to the directory it names; `GIT_COMMON_DIR` takes the first three back, but git's ref
    store reads the file whatever the environment says (measured: the refs then come from the planted directory
    and the objects from this one). It also refuses one whose `refs`, `packed-refs`, `logs`, `objects`, `HEAD` or
    `config` is a symbolic link: git reads refs through the first two as it does through `commondir`, and a delete
    rewrites `packed-refs` and removes a file under `logs` *through* the link. No canonical of ours has either, so
    one that does is not read and not written. This is a check before the process starts, not a lock: it catches a
    planted file, not a race."""
    if os.path.lexists(os.path.join(repo, "commondir")):
        raise Unreadable("the canonical holds a `commondir` file, which would have its refs read from another "
                         "directory; nothing was read")
    for part in _NOT_A_LINK:
        if os.path.islink(os.path.join(repo, part)):
            raise Unreadable("the canonical's `%s` is a symbolic link, so git would read or write somewhere "
                             "else; nothing was read" % part)
    return {"env": git_env(repo)}


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
