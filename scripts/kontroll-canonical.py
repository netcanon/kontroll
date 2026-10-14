#!/usr/bin/env python3
"""Say what is in a canonical that this tool did not put there (C10). Read-only: it reports, it changes nothing.

The canonical (`/srv/kontroll.git`) is made by `ansible/playbooks/local-canonical.yml`, and from then on written
by three things: that play's mirror, the promote, and — when a deploy is armed — the network service, which
holds the whole directory for writing. What the tool's own code writes there is two kinds of ref and the
objects they name. Everything else in the directory was put there by a person, or by somebody using the
service's uid; and a bare repository has many places where one more file changes what git does: a config key, a
hook, a `commondir`, a graft file, a reflog, a replace ref, an alternates file. The gits this tool starts there
run in an environment of their own (`kontroll.canon`, which says what that answers and what it does not). This
command is how a PERSON finds out that one of those files is there.

    python3 -B scripts/kontroll-canonical.py --audit /srv/kontroll.git            # report; exit 0
    python3 -B scripts/kontroll-canonical.py --audit /srv/kontroll.git --strict   # exit 3 when it reports anything

(Run from the checkout: the script's path is relative to it, and the hook the canonical's is compared with is the
one that checkout ships. `-B`, because root may be who runs it and the checkout is somebody else's.)

IT READS THE REPOSITORY AS FILES, AND EVERY STEP INTO IT IS TAKEN FROM AN OPEN DIRECTORY. The directory it is
reading is one somebody else is writing while it reads. A path is looked up again, component by component,
every time it is used: `lstat("refs")` says "a directory", the writer exchanges `refs` for a link, and
`listdir("refs")` lists whatever the link names. So where the platform has them (every node: Linux), the top
directory is opened once and each directory below it is opened FROM its parent's descriptor, with `O_NOFOLLOW`
and `O_DIRECTORY`; a file is opened from the descriptor of the directory it was listed in, with `O_NOFOLLOW` and
without waiting, and is read only when the thing that was opened is a regular file. What is listed and what is
read is then inside the repository whatever is exchanged meanwhile. (Windows has no such descriptors; there the
same functions work on paths. Nothing writes a canonical on Windows — the unit rows run there.)

It starts ONE process: `git config --file - --no-includes --list -z`, which is handed the bytes of the `config`
file on its standard input and prints the keys in it. That git is never told where the repository is and opens
nothing of it. `--no-includes` is not decoration: asked of its standard input, git FOLLOWS an `include.path`
(measured — it lists the keys of the file the line names, and opens whatever that is), which it does not when
it is asked of a path. Git is used at all because the question is "which keys would GIT read here", and only
git's own parser answers that. The bytes are bounded before git sees them — in size and in the length of a
line — because git prints a section's name once for every key under it: one long section header and many short
keys make a small file into gigabytes of output. Started by root for a repository the service can write, that
git runs as the service uid, as the tool's own gits there do (`canon.git_kwargs`).

WHAT IT PRINTS is one line per finding, `<kind>: <name>` — a KIND and a KEY, never a value: the name of a config
key and not what it is set to, the name of a ref and not the commit, the name of a hook and not its text. A name
is whatever somebody wrote: it is shown escaped where it is not plain text (a backslash always doubled, so one
line is one name), and cut at a length. Each kind is cut at a number of lines of its own, so that many lines of
one kind cannot push another kind out of the report. Nothing is printed for a canonical that holds only what
the play and the tool write.

  config-key            a key of `config` other than the ones `git init --bare` and the play write
  config-value          one of the keys the play sets (and `core.bare`) is missing, is there more than once, or
                        is not set to what the play sets it to. Which value it has is not printed
  config-include        an `include.path` / `includeIf.*.path` — a second config file, which this audit did not read
  config-unread         `config` is missing, is not a regular file, cannot be read by the caller, is too large
                        (or has a line too long), or git could not list it
  hook                  an entry of `hooks/` other than `update` and git's own `*.sample` files
  hook-changed          `hooks/update` is not the file this checkout ships
  hook-off              `hooks/update` is there and is not executable: git runs no `update` hook at all
  hook-writable         `hooks/` or `hooks/update` is the network service's to write (the play makes both root's)
  hook-missing          there is no `hooks/update`
  info-entry            an entry of `info/` other than `exclude`
  objects-info-entry    an entry of `objects/info/` other than `packs`
  branches-entry        an entry of `branches/` (git's old place for a remote's shorthand; the play leaves it empty)
  top-entry             an entry of the top directory that a bare repository made by the play does not have
  reflog                a file under `logs/` other than the reflogs git keeps for `HEAD`, `refs/heads/main` and
                        `refs/heads/proposed/<run id>` (a promote runs git in the tool's own environment, and
                        there git keeps one for the refs it moves: those are the tool's own doing)
  ref                   a ref other than `refs/heads/main` and `refs/heads/proposed/<run id>` (a file under `refs/`)
  packed-ref            the same, listed in `packed-refs`
  symbolic-ref          a ref that names another ref instead of a commit
  head                  `HEAD` does not name `refs/heads/main`, or is not there
  link                  a symbolic link where a file or a directory is expected
  not-a-file            something that is not what its place asks for and is no link (a pipe, a socket, a device;
                        a directory where a file belongs, a file where a directory does)
  unread                a part of the repository that could not be listed or read, is larger or deeper than this
                        reads; a line of `packed-refs` git would not take for a ref; lines of the report that
                        are not shown
  note                  an entry of the top directory that git leaves by itself (`FETCH_HEAD`, `ORIG_HEAD`,
                        `gc.log`) — named, and not a finding

What git's own garbage collection would add by itself — `info/refs` and `objects/info/commit-graph`; a push
starts a collection once enough has gathered — the play switches off with keys of its own
(`gc.writeCommitGraph`, `repack.updateServerInfo`). So in a canonical the play made, either file is somebody's,
and a finding like any other entry there.

It does not read the object store: of `objects/` it names a symbolic link in the directory itself and in
`objects/pack/` (through one, git reads objects from somewhere else, as through an alternates file), and nothing
of a pack or an object.

Exit 0 with the report (also when there are findings: the deploy prints them and goes on). With `--strict`, exit
3 when there is at least one finding. Exit 2 for a usage error, or when the path is not a directory. Exit 1
when the audit ended on an error of its own: what it printed is then not the whole report, and its last line
says so.
"""
import argparse
import hashlib
import itertools
import os
import re
import stat
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # so `from kontroll import canon` resolves
from kontroll import canon

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED_HOOK = os.path.join(ROOT, "ansible", "playbooks", "files", "canonical-update-hook.sh")

# The top directory of a bare repository as `git init --bare` and the play leave it — and `logs`, which the
# first promote makes (`_reflogs` looks inside it) …
_TOP = frozenset({b"HEAD", b"config", b"description", b"hooks", b"info", b"objects", b"refs", b"packed-refs",
                  b"branches", b"logs"})
# … and what git itself leaves there in the course of ordinary work: named, not findings.
_TOP_NOTED = frozenset({b"FETCH_HEAD", b"ORIG_HEAD", b"gc.log"})
# The keys `git init --bare` writes (some of them only on a file system that asks for them, or for a repository
# that is not sha1) and the keys the play sets. As git prints a key: section and name in lower case.
_CONFIG = frozenset({"core.repositoryformatversion", "core.filemode", "core.bare", "core.sharedrepository",
                     "receive.denynonfastforwards", "receive.denydeletes",
                     "gc.writecommitgraph", "repack.updateserverinfo",
                     "core.symlinks", "core.ignorecase", "core.precomposeunicode", "extensions.objectformat"})
# What the play sets each of its keys to, and what `git init --bare` sets `core.bare` to. These are the keys of
# the closed set that are COMPARED: missing, there more than once, or saying something else is a line of the
# report. (The play sets its own again on every deploy — after the audit, which is how a key somebody turned in
# between gets named at all.)
_PLAY_VALUES = {"receive.denynonfastforwards": b"true", "receive.denydeletes": b"true",
                "core.sharedrepository": b"group", "core.bare": b"true",
                "gc.writecommitgraph": b"false", "repack.updateserverinfo": b"false"}

_NAME_CHARS = 200             # a name is cut here: it is somebody's text, and a line of a deploy's output
_MAX_PER_KIND = 25            # lines of one kind; the rest of that kind are counted, and no other kind pays for them
_MAX_ENTRIES = 20000          # entries looked at in one directory, and under one directory tree
_MAX_DEPTH = 16               # directories below `refs/` or `logs/`; a ref of ours is three deep
_CONFIG_BYTES = 64 << 10      # a canonical's config is a few hundred bytes
_CONFIG_LINE = 1024           # … and no line of it is long (see `_config`: what a long section header costs)
_PACKED_BYTES = 8 << 20
_REF_BYTES = 256
_HOOK_BYTES = 1 << 20
_GIT_SPACE = b" \t\n\r"       # what git's own `isspace` takes for a space: not a vertical tab, not a form feed

_PACKED_HEADER = b"# pack-refs with: "   # git's own first line; any other line that begins with `#` git refuses
_PACKED_LINE = re.compile(rb"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?[ \t\r]")  # git reads an object name
_REF_VALUE = re.compile(rb"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\n")            # case-insensitively

# Can a directory be opened from its parent's descriptor, and listed and searched by its own? (Linux, the BSDs,
# macOS: yes. Windows: no.)
_AT = (hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd
       and os.stat in os.supports_dir_fd and os.scandir in os.supports_fd)


class _Report:
    """The findings and notes of one audit, each `(kind, name)` with the name already made text."""

    def __init__(self):
        self.findings, self.notes = [], []

    def add(self, kind, name=b""):
        text, exact = canon.show(name if isinstance(name, bytes) else str(name).encode("utf-8", "replace"))
        if exact:                                       # `show` doubles a backslash only where it escapes: here
            text = text.replace("\\", "\\\\")           # it is doubled always, so `a\nb` is never two names
        if len(text) > _NAME_CHARS:
            text = text[:_NAME_CHARS] + "…"
        (self.notes if kind == "note" else self.findings).append((kind, text))

    def lines(self):
        out = []
        for group in (self.findings, self.notes):
            by_kind = {}
            for kind, text in sorted(set(group)):
                by_kind.setdefault(kind, []).append(text)
            for kind in sorted(by_kind):
                texts = by_kind[kind]
                out += ["%s: %s" % (kind, text) if text else kind for text in texts[:_MAX_PER_KIND]]
                if len(texts) > _MAX_PER_KIND:
                    out.append("unread: %d more `%s` lines are not shown" % (len(texts) - _MAX_PER_KIND, kind))
        return out


def _what(mode):
    """What a `st_mode` describes: "link", "dir", "file" or "other"."""
    if stat.S_ISLNK(mode):
        return "link"
    if stat.S_ISDIR(mode):
        return "dir"
    return "file" if stat.S_ISREG(mode) else "other"


class _Dir:
    """One directory of the repository, HELD OPEN: everything asked of it is asked of the directory that was
    opened, not of a path that is looked up again (see the module's second paragraph). `fd` is None where the
    platform cannot do that; `path` is then what is used, and is otherwise kept for nothing."""

    def __init__(self, path, fd=None):
        self.path, self.fd = path, fd

    @classmethod
    def top(cls, path):
        """The repository's own directory — the one path the CALLER named, so a link in it is followed as the
        caller meant — or None when it cannot be opened."""
        if not _AT:
            return cls(path) if os.path.isdir(path) else None
        try:
            return cls(path, os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)))
        except OSError:
            return None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def stat(self):
        """The `stat` of this directory itself — the one that is open — or None."""
        try:
            return os.fstat(self.fd) if self.fd is not None else os.lstat(self.path)
        except OSError:
            return None

    def names(self, limit=None):
        """`(names, more)`: up to `limit` (`_MAX_ENTRIES` when none is given) of the names in this directory,
        as bytes, sorted, and whether there are more than that — or None when it cannot be listed. It is
        listed once."""
        limit = max(_MAX_ENTRIES if limit is None else limit, 0)
        try:
            with os.scandir(self.fd if self.fd is not None else self.path) as entries:
                got = [os.fsencode(entry.name) for entry in itertools.islice(entries, limit + 1)]
        except OSError:
            return None
        return sorted(got[:limit]), len(got) > limit

    def kind(self, name):
        """What the entry `name` IS, without following it: "file", "dir", "link", "other"; None when nothing
        is there; "unreadable" when it cannot be looked at (the caller says so: it is not "nothing there")."""
        try:
            if self.fd is not None:
                return _what(os.lstat(name, dir_fd=self.fd).st_mode)
            return _what(os.lstat(os.path.join(self.path, name)).st_mode)
        except FileNotFoundError:
            return None
        except OSError:
            return "unreadable"

    def sub(self, name):
        """The directory `name` of this one, opened from this one and never through a link — or None: it is
        not there, is not a directory, is a link, or cannot be opened."""
        if self.fd is None:
            path = os.path.join(self.path, name)
            return _Dir(path) if self.kind(name) == "dir" else None
        flags = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
                 | getattr(os, "O_CLOEXEC", 0))
        try:
            return _Dir(os.path.join(self.path, name), os.open(name, flags, dir_fd=self.fd))
        except OSError:
            return None

    def read(self, name, limit):
        """`(bytes, stat)` of the regular file `name` of this directory — the bytes and the `stat` of the ONE
        thing that was opened — or `(None, None)`: it is not there, is not a regular file (a link is not
        followed, a pipe is not waited on), cannot be read, or is larger than `limit`."""
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        try:
            if self.fd is not None:
                fd = os.open(name, flags | getattr(os, "O_CLOEXEC", 0), dir_fd=self.fd)
            elif self.kind(name) != "file":               # no O_NOFOLLOW here: the look is the only check
                return None, None
            else:
                fd = os.open(os.path.join(self.path, name), flags)
        except OSError:
            return None, None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return None, None
            data = os.read(fd, limit + 1)
            while data and len(data) <= limit:
                more = os.read(fd, limit + 1 - len(data))
                if not more:
                    break
                data += more
            return (data, st) if len(data) <= limit else (None, None)
        except OSError:
            return None, None
        finally:
            os.close(fd)


def _expect(report, here, name, kind, shown=None):
    """The entry `name` of the open directory `here`, as an entry the play's repository has: True when it is
    there and is a `kind`. A link, or a thing of another kind, in its place is a finding, and so is one that
    cannot be looked at; nothing there is not (a repository need not have every one of them). `shown` is the
    name the report gives it (its path from the top directory) when that is not `name`."""
    is_a = here.kind(name)
    if is_a == kind:
        return True
    if is_a == "link":
        report.add("link", shown or name)
    elif is_a == "unreadable":
        report.add("unread", shown or name)
    elif is_a is not None:
        report.add("not-a-file", shown or name)
    return False


def _enter(report, here, name, shown=None):
    """The directory `name` of the open directory `here`, OPEN — or None, with the finding that says why when
    something other than a directory is there (`_expect`). The caller closes it."""
    if not _expect(report, here, name, "dir", shown):
        return None
    below = here.sub(name)
    if below is None:                                    # it was a directory a moment ago: somebody is writing
        report.add("unread", shown or name)
    return below


def _listed(report, here, shown):
    """The names in the open directory `here` (at most `_MAX_ENTRIES`; more than that is said), or None."""
    listed = here.names()
    if listed is None:
        report.add("unread", shown)
        return None
    names, more = listed
    if more:
        report.add("unread", shown + b" (more entries than this reads)")
    return names


def _config(report, top, repo):
    """The keys of `config`, as git reads that file — handed to git as bytes on standard input.

    THE BYTES ARE BOUNDED FIRST. git prints `<section>.<name>` for every key, so a section header of half a
    megabyte followed by a hundred thousand two-byte lines is a file of 700 kB that git answers with 46 GB
    (measured: 220 kB gave 1.9 GB) — into the memory of a process that is root on the control node, in the
    middle of a deploy. With no line longer than `_CONFIG_LINE` and no file larger than `_CONFIG_BYTES`, git's
    answer is at most a few tens of megabytes."""
    if not _expect(report, top, b"config", "file"):
        report.add("config-unread", b"config")
        return
    data, _st = top.read(b"config", _CONFIG_BYTES)
    if data is None or any(len(line) > _CONFIG_LINE for line in data.split(b"\n")):
        report.add("config-unread", b"config")
        return
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "TEMP", "TMP") if k in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
               GIT_CEILING_DIRECTORIES=os.path.abspath(os.sep))
    try:
        p = subprocess.run(["git", "config", "--file", "-", "--no-includes", "--list", "-z"], input=data,
                           capture_output=True, timeout=20, env=env, cwd=os.path.abspath(os.sep),
                           **canon.identity_kwargs(os.fsdecode(repo)))
    except (OSError, subprocess.SubprocessError, canon.Unreadable):
        report.add("config-unread", b"config")
        return
    if p.returncode != 0:
        report.add("config-unread", b"config")
        return
    # `-z`: each record is the key, a newline, the value, a NUL (a key with no value has no newline). A value
    # is compared and never printed.
    values = {}
    for record in p.stdout.split(b"\0"):
        if record:
            key, _nl, value = record.partition(b"\n")
            # The value byte for byte, never normalised: the play writes each key in one form (`true`,
            # `group`, `false`), and a value git reads another way, or will not take at all
            # (`core.sharedRepository = GROUP`, `receive.denyDeletes = " true"`), is "not what the play
            # set" — the audit's own contract. The KEY is still as git prints it, in lower case.
            values.setdefault(key, []).append(value if _nl else None)
    for key in sorted(values):
        low = key.lower()
        if low == b"include.path" or (low.startswith(b"includeif.") and low.endswith(b".path")):
            report.add("config-include", key)
        elif key.decode("utf-8", "replace") not in _CONFIG:
            report.add("config-key", key)
    for name, value in sorted(_PLAY_VALUES.items()):
        if values.get(name.encode("ascii")) != [value]:
            report.add("config-value", name)


def _services(st):
    """Is the thing this `stat` describes the network service's to write (its own; its group's, with the
    group's write bit; anybody's)? Asked of `hooks/` only, and only by a caller who is not that uid."""
    return (st.st_uid == canon.SERVICE_UID or (st.st_gid == canon.SERVICE_GID and bool(st.st_mode & 0o020))
            or bool(st.st_mode & 0o002))


def _hooks(report, top):
    """`hooks/`: the shipped `update`, git's own samples, and nothing else — and `update` a hook git RUNS
    (git skips, with a hint nobody sees, a hook that is not executable), in a directory the service cannot
    write. The file's bytes, its mode and its owner are those of one open file."""
    hooks = _enter(report, top, b"hooks")
    if hooks is None:
        report.add("hook-missing", b"update")
        return
    with hooks:
        posix = hasattr(os, "geteuid")
        asks = posix and os.geteuid() != canon.SERVICE_UID      # the service asking about itself learns nothing
        st = hooks.stat()
        if asks and st is not None and _services(st):
            report.add("hook-writable", b"hooks")
        names = _listed(report, hooks, b"hooks")
        if names is None:
            return
        for name in names:
            if name == b"update":
                continue
            if not name.endswith(b".sample"):
                report.add("hook", name)
            elif hooks.kind(name) != "file":
                _expect(report, hooks, name, "file", b"hooks/" + name)
        is_a = hooks.kind(b"update")
        if is_a is None:
            report.add("hook-missing", b"update")
            return
        if is_a != "file":
            _expect(report, hooks, b"update", "file", b"hooks/update")
        try:
            with open(SHIPPED_HOOK, "rb") as fh:
                ours = fh.read(_HOOK_BYTES + 1)
        except OSError:
            ours = None
        theirs, st = hooks.read(b"update", _HOOK_BYTES)
        if ours is None or theirs is None or hashlib.sha256(ours).digest() != hashlib.sha256(theirs).digest():
            report.add("hook-changed", b"update")
        if st is not None and posix and not st.st_mode & 0o111:
            report.add("hook-off", b"update")
        if st is not None and asks and _services(st):
            report.add("hook-writable", b"update")


def _only(report, here, shown, allowed, kind):
    """Every entry of the open directory `here` (`shown`: its path from the top) that is not in `allowed` is
    a `kind` finding. An entry that is allowed must still be a regular file."""
    for name in _listed(report, here, shown) or ():
        if name in allowed:
            _expect(report, here, name, "file", shown + b"/" + name)
        else:
            report.add(kind, name)


def _only_in(report, top, where, allowed, kind):
    """`_only` for the directory `where` of the top directory, when there is one."""
    below = _enter(report, top, where)
    if below is not None:
        with below:
            _only(report, below, where, allowed, kind)


def _ours(refname):
    """Is `refname` (bytes) one of the two kinds of ref this tool writes?"""
    if refname == canon.MAIN.encode():
        return True
    prefix = canon.PROPOSED.encode()
    try:
        return refname.startswith(prefix) and canon.valid_run_id(refname[len(prefix):].decode("ascii"))
    except UnicodeDecodeError:
        return False


def _files_below(report, top, name):
    """Every regular file below the directory `name` of the open directory `top`, as `(path from the top
    directory, the open directory it is in, its name there)` — so the caller reads it from that directory. A
    link and a thing that is neither file nor directory are findings on the way and are not gone into; a
    directory that cannot be listed, a tree with more entries or more levels than this reads, are `unread`.
    One directory is open per level, `_MAX_DEPTH` at most."""
    first = _enter(report, top, name)
    if first is None:
        return
    left = _MAX_ENTRIES
    stack = [(first, name, None)]
    try:
        while stack:
            here, rel, names = stack[-1]
            if names is None:
                listed = here.names(left)
                if listed is None:
                    report.add("unread", rel)
                    stack.pop()[0].close()
                    continue
                found, more = listed
                left -= len(found)
                if more:
                    report.add("unread", name + b" (more entries than this reads)")
                names = iter(found)
                stack[-1] = (here, rel, names)
            entry = next(names, None)
            if entry is None:
                stack.pop()[0].close()
                continue
            path = rel + b"/" + entry
            is_a = here.kind(entry)
            if is_a == "file":
                yield path, here, entry
            elif is_a == "link":
                report.add("link", path)
            elif is_a == "other":
                report.add("not-a-file", path)
            elif is_a == "unreadable":
                report.add("unread", path)
            elif is_a == "dir":
                if len(stack) >= _MAX_DEPTH:
                    report.add("unread", name + b" (deeper than this reads)")
                    continue
                below = here.sub(entry)
                if below is None:
                    report.add("unread", path)
                else:
                    stack.append((below, path, None))
    finally:
        for here, _rel, _names in stack:
            here.close()


def _loose_refs(report, top):
    """Every file under `refs/`: its name, whether it names another ref, and — for a ref of ours — whether it
    holds what git writes into a ref file (an object name and a newline). Which object is not printed."""
    for rel, here, name in _files_below(report, top, b"refs"):
        ours = _ours(rel)
        if not ours:
            report.add("ref", rel)
        text, _st = here.read(name, _REF_BYTES)
        if text is None:
            report.add("unread", rel)
        elif text.startswith(b"ref:"):
            report.add("symbolic-ref", rel)
        elif ours and not _REF_VALUE.fullmatch(text):
            report.add("unread", rel)


def _reflogs(report, top):
    """`logs/`, when it is there: the reflogs of `HEAD` and of the tool's two kinds of ref are git's own
    record of what the tool did; any other file under it is named. Nothing of a reflog is read."""
    for rel, _here, _name in _files_below(report, top, b"logs"):
        of = rel[len(b"logs/"):]
        if of != b"HEAD" and not _ours(of):
            report.add("reflog", rel)


def _packed_refs(report, top):
    """`packed-refs`, read as text the way git reads it: a first line that is git's own header; then one ref a
    line — an object name, ONE space, tab or carriage return, and the name, which is the rest of the line; and,
    under a ref that is a tag, a line that begins with `^` (what the tag points at). Every other line is one
    git refuses the whole file for, and is said — by its place, never by its text. So is a `^` line with no ref
    above it, or under one of the tool's own refs: those name commits and never have one, and such a line is
    what is left when a tag's line was taken out by hand and the line under it was not (git hangs it on the ref
    above)."""
    if not _expect(report, top, b"packed-refs", "file"):
        return
    data, _st = top.read(b"packed-refs", _PACKED_BYTES)
    if data is None:
        report.add("unread", b"packed-refs")
        return
    lines = data.split(b"\n")
    if lines[-1] == b"":                                 # what follows the last newline is not a line
        lines.pop()
    peelable = False                                     # may the next line be a `^<id>`? Under a tag only
    for at, line in enumerate(lines):
        if at == 0 and line.startswith(_PACKED_HEADER):
            continue
        if line.startswith(b"^") and peelable:
            peelable = False
            continue
        peelable = False
        head = None if line.startswith(b"^") else _PACKED_LINE.match(line)
        refname = line[head.end():] if head else b""
        if not refname:
            report.add("unread", b"packed-refs (a line that is not a ref)")
        elif not _ours(refname):
            report.add("packed-ref", refname)
            peelable = True


def _head(report, top):
    """`HEAD` names `refs/heads/main`, as git reads it: `ref:`, git's own spaces, the name, git's own spaces.
    (Python's `split()` also takes a vertical tab and a form feed for a space; git does not, and a `HEAD` that
    ends in one names a ref that cannot exist.)"""
    if not _expect(report, top, b"HEAD", "file"):
        if top.kind(b"HEAD") is None:                    # no `HEAD` at all: to git this is then no repository
            report.add("head", b"HEAD")
        return
    text, _st = top.read(b"HEAD", _REF_BYTES)
    if text is None or not text.startswith(b"ref:") or text[4:].strip(_GIT_SPACE) != canon.MAIN.encode():
        report.add("head", b"HEAD")


def _objects(report, top):
    """`objects/`: the entries of `objects/info/`, and any symbolic link in `objects/` itself or in
    `objects/pack/` — through one, git reads objects from another directory, which is what an alternates file
    does. Nothing of an object or a pack is read."""
    objects = _enter(report, top, b"objects")
    if objects is None:
        return
    with objects:
        for name in _listed(report, objects, b"objects") or ():
            if objects.kind(name) == "link":
                report.add("link", b"objects/" + name)
        for sub in (b"info", b"pack"):
            below = _enter(report, objects, sub, b"objects/" + sub)
            if below is None:
                continue
            with below:
                if sub == b"info":
                    _only(report, below, b"objects/info", (b"packs",), "objects-info-entry")
                else:
                    for name in _listed(report, below, b"objects/pack") or ():
                        if below.kind(name) == "link":
                            report.add("link", b"objects/pack/" + name)


def audit(repo):
    """The report for the repository at `repo` (a path): a `_Report`. Raises nothing for what the repository
    holds — every part that cannot be read is itself a line of the report."""
    repo = os.fsencode(os.path.abspath(repo))
    report = _Report()
    top = _Dir.top(repo)
    if top is None:
        report.add("unread", b"the top directory")
        return report
    with top:
        for name in _listed(report, top, b"the top directory") or ():
            if name in _TOP_NOTED:
                if _expect(report, top, name, "file"):
                    report.add("note", name)
            elif name not in _TOP:
                report.add("top-entry", name)
        _expect(report, top, b"description", "file")
        _only_in(report, top, b"branches", (), "branches-entry")
        _only_in(report, top, b"info", (b"exclude",), "info-entry")
        _config(report, top, repo)
        _hooks(report, top)
        _objects(report, top)
        _head(report, top)
        _loose_refs(report, top)
        _packed_refs(report, top)
        _reflogs(report, top)
    return report


def main(argv):
    ap = argparse.ArgumentParser(description="Say what is in a canonical that this tool did not put there (C10). "
                                             "Read-only.")
    ap.add_argument("--audit", required=True, metavar="<repo>", help="the canonical: a bare repository's directory")
    ap.add_argument("--strict", action="store_true", help="exit 3 when there is at least one finding")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        if e.code in (0, None):
            raise
        return 2
    if not os.path.isdir(args.audit):
        sys.stderr.write("kontroll-canonical: %s is not a directory; nothing was audited\n" % canon.show(
            os.fsencode(args.audit))[0][:_NAME_CHARS])
        return 2
    try:
        report, ended = audit(args.audit), None
    except Exception as e:      # noqa: BLE001 — a deploy prints what this printed: a traceback there says nothing
        report, ended = _Report(), type(e).__name__
    lines = report.lines()
    if ended:
        lines.append("unread: the audit ended on an error of its own (%s); this is not a report of the canonical"
                     % ended)
    try:
        for line in lines:
            try:
                print(line)
            except UnicodeEncodeError:
                # `\uNNNN`, never `\xNN`: in a name, `canon.show` spells a BYTE that is not UTF-8 as `\xNN`,
                # and a character must not read like one.
                print("".join(ch if ch < "\x7f" else ("\\u%04x" if ord(ch) < 0x10000 else "\\U%08x") % ord(ch)
                              for ch in line))
        if sys.stdout is not None:
            sys.stdout.flush()
    except OSError:                                      # nobody is reading (a closed pipe, a full disk)
        try:
            sys.stdout = open(os.devnull, "w")           # so the interpreter's own flush at exit says nothing
        except OSError:
            pass
        return 1
    if ended:
        return 1
    return 3 if args.strict and report.findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
