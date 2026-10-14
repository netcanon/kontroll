"""scripts/kontroll-canonical.py --audit — what is in a canonical that this tool did not put there (C10).

WHY: a bare repository has many places where one more file changes what git does there — a config key, a hook, a
`commondir`, a graft file, a reflog, a replace ref, an alternates file — and when a deploy is armed the network
service may write every one of them. The tool's own git answers them (`kontroll.canon`); this command is how a
PERSON learns that one is there. So it has to hold three things, and these rows are them:

  * it NAMES each such thing, by kind and key — and a canonical the play made, used the way the tool uses it, is
    reported clean (a report that cries at nothing is not read twice);
  * it prints no VALUE: the names are somebody's text and go into a deploy's log, escaped and cut; what a key is
    set to, what a hook says, which commit a ref names, do not;
  * reading a hostile directory is itself safe: no link is followed, nothing that is not a regular file is
    opened (a pipe named `HEAD` must not hang a deploy), and the one process it starts — git, for the names of
    the config keys — is handed the file's bytes and is never told where the repository is.
"""
import ast
import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
import threading

import pytest
import yaml

import _canon_repo as R
from kontroll import canon, gitio
from kontroll.service import discard

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("isolated_git")]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, "scripts", "kontroll-canonical.py")
PLAY = os.path.join(ROOT, "ansible", "playbooks", "local-canonical.yml")
HOOK = os.path.join(ROOT, "ansible", "playbooks", "files", "canonical-update-hook.sh")
SECRET = "VALUE-nobody-should-see-7f3a"


def _load():
    spec = importlib.util.spec_from_file_location("kontroll_canonical", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tool = _load()


def _play_tasks():
    plays = yaml.safe_load(open(PLAY, encoding="utf-8"))
    return next(p for p in plays if "tasks" in p)["tasks"]


def _harden():
    return next(t for t in _play_tasks() if t["name"].startswith("Harden the bare repo"))


def _set_as_the_play_does(bare, item):
    """Run the play's own `git config` command for one item of its loop. The command line is READ FROM THE
    PLAY — its words after `git -C <the canonical>` — so a flag it gains or loses is the flag these rows run."""
    cmd = _harden()["ansible.builtin.command"]["cmd"]
    for told, value in (("{{ bare }}", "."), ("{{ item.key }}", item["key"]), ("{{ item.value }}", item["value"])):
        assert told in cmd, (told, cmd)
        cmd = cmd.replace(told, value)
    words = cmd.split()
    assert words[:3] == ["git", "-C", "."] and "{" not in cmd, cmd
    R.git(bare, *words[3:])


def _as_the_play_makes_it(tmp_path):
    """`_canon_repo.build` — a bare repository with `main` and two proposals of minted ids — finished the way
    `local-canonical.yml` finishes one: `HEAD` as its copy task writes it, every key of its config loop set by
    its own command line (both READ FROM THE PLAY, so a key the play gains is set here too), and the shipped
    `update` hook with the mode the play's copy task gives it."""
    r = R.build(tmp_path)
    tasks = _play_tasks()
    head = next(t for t in tasks if t["name"].startswith("Point the bare repo's HEAD at main"))
    with open(os.path.join(r.bare, "HEAD"), "w", encoding="utf-8", newline="") as fh:
        fh.write(head["ansible.builtin.copy"]["content"])
    for item in _harden()["loop"]:
        _set_as_the_play_does(r.bare, item)
    shutil.copyfile(HOOK, os.path.join(r.bare, "hooks", "update"))
    hook = next(t for t in tasks if t["name"].startswith("Install the C10 update-hook"))
    os.chmod(os.path.join(r.bare, "hooks", "update"), int(hook["ansible.builtin.copy"]["mode"], 8))
    return r


def _report(bare, *flags, capsys):
    rc = tool.main(["--audit", bare] + list(flags))
    out = capsys.readouterr().out
    return rc, out.splitlines()


def _put(bare, rel, text="x\n"):
    path = os.path.join(bare, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def test_a_canonical_the_play_made_and_the_tool_used_is_clean(tmp_path, capsys):
    """A bare repository finished as the play finishes one — its `HEAD`, the keys of its config loop, the shipped
    hook — holding `main` and two staged proposals: the audit prints nothing and exits 0, with `--strict` too.
    It still prints nothing after the tool has USED it — a promote (the real one, which leaves git's reflog of
    `HEAD` and of `main` under `logs/`) and a discard — and after git has COLLECTED GARBAGE there, which packs
    the refs and, without the play's keys for it, leaves a list of refs in `info/` and a commit-graph. And every
    key of the play's loop is in the audit's closed set, with the value the audit expects it to have.

    Guards the report being worth reading. The play's keys are READ from the play: a key it gains tomorrow is
    set in this repository, and this row fails until the audit knows it. What the tool's own promote leaves is
    not a finding, and neither is anything git's own housekeeping leaves — a push starts a collection by
    itself once enough has gathered, so otherwise every node would print the same lines on every deploy, and
    the line that matters would be read past."""
    r = _as_the_play_makes_it(tmp_path)
    assert _report(r.bare, capsys=capsys) == (0, [])
    assert _report(r.bare, "--strict", capsys=capsys) == (0, [])
    assert gitio.promote_commit(R.FF, r.ff, r.main, r.bare) is True
    assert discard.discard_proposal(R.STALE, canonical=r.bare)["ok"] is True
    capsys.readouterr()
    assert os.path.isfile(os.path.join(r.bare, "logs", "refs", "heads", "main")), "control: the promote left a reflog"
    assert _report(r.bare, "--strict", capsys=capsys) == (0, [])
    R.git(r.bare, "gc", "-q")
    assert os.path.isfile(os.path.join(r.bare, "packed-refs")), "control: the collection ran (it packed the refs)"
    assert _report(r.bare, "--strict", capsys=capsys) == (0, [])
    loop = _harden()["loop"]
    assert {item["key"].lower() for item in loop} <= tool._CONFIG
    assert {item["key"].lower(): item["value"].encode() for item in loop} == {
        k: v for k, v in tool._PLAY_VALUES.items() if k != "core.bare"}


def _config(key, value=SECRET):
    return lambda bare: R.git(bare, "config", key, value)


def _rewrite(rel, text):
    return lambda bare: _put(bare, rel, text)


def _append(rel, text):
    def plant(bare):
        with open(os.path.join(bare, *rel.split("/")), "a", encoding="utf-8", newline="") as fh:
            fh.write(text)
    return plant


def _remove(rel):
    return lambda bare: os.unlink(os.path.join(bare, *rel.split("/")))


_ID = "0" * 39 + "1"

_PLANTED = [
    ("a-key-that-names-a-program", _config("core.hooksPath"), ["config-key: core.hookspath"]),
    ("a-key-with-a-subsection", _config("url.%s.insteadOf" % "https://example.test/x"),
     ["config-key: url.https://example.test/x.insteadof"]),
    ("an-include", _config("include.path"), ["config-include: include.path"]),
    ("an-include-of-a-file-that-is-there", lambda bare: (
        _put(bare, "elsewhere.cfg", "[extra]\n\tfromInclude = %s\n" % SECRET),
        R.git(bare, "config", "include.path", os.path.join(bare, "elsewhere.cfg").replace(os.sep, "/"))),
     ["config-include: include.path", "top-entry: elsewhere.cfg"]),
    ("a-conditional-include", _config("includeIf.gitdir:/srv/.path"), ["config-include: includeif.gitdir:/srv/.path"]),
    ("a-config-git-cannot-read", _rewrite("config", "[core\n\tbare = true\n" + SECRET), ["config-unread: config"]),
    ("no-config", _remove("config"), ["config-unread: config"]),
    ("another-hook", _rewrite("hooks/reference-transaction", "#!/bin/sh\necho %s\n" % SECRET),
     ["hook: reference-transaction"]),
    ("the-hook-changed", _rewrite("hooks/update", "#!/bin/sh\n# %s\nexit 0\n" % SECRET), ["hook-changed: update"]),
    ("the-hook-gone", _remove("hooks/update"), ["hook-missing: update"]),
    ("a-graft-file", _rewrite("info/grafts", SECRET + "\n"), ["info-entry: grafts"]),
    ("an-attributes-file", _rewrite("info/attributes", "* filter=%s\n" % SECRET), ["info-entry: attributes"]),
    ("an-alternates-file", _rewrite("objects/info/alternates", "/%s\n" % SECRET), ["objects-info-entry: alternates"]),
    ("a-chain-of-commit-graphs", _rewrite("objects/info/commit-graphs/commit-graph-chain", SECRET),
     ["objects-info-entry: commit-graphs"]),
    ("a-remote-in-the-old-place", _rewrite("branches/origin", "https://example.test/%s\n" % SECRET),
     ["branches-entry: origin"]),
    ("a-commondir", _rewrite("commondir", "/%s\n" % SECRET), ["top-entry: commondir"]),
    ("a-shallow-file", _rewrite("shallow", _ID + "\n"), ["top-entry: shallow"]),
    ("a-reflog-of-a-ref-that-is-not-ours", _rewrite("logs/refs/heads/release", SECRET + "\n"),
     ["reflog: logs/refs/heads/release"]),
    ("a-file-under-logs-that-is-no-reflog", _rewrite("logs/note", SECRET + "\n"), ["reflog: logs/note"]),
    ("a-tag-called-main", _rewrite("refs/tags/main", _ID + "\n"), ["ref: refs/tags/main"]),
    ("a-ref-directly-under-refs", _rewrite("refs/main", _ID + "\n"), ["ref: refs/main"]),
    ("a-replace-ref", _rewrite("refs/replace/" + _ID, _ID + "\n"), ["ref: refs/replace/" + _ID]),
    ("a-branch-that-is-not-ours", _rewrite("refs/heads/release", _ID + "\n"), ["ref: refs/heads/release"]),
    ("a-proposal-of-no-minted-id", _rewrite("refs/heads/proposed/next", _ID + "\n"),
     ["ref: refs/heads/proposed/next"]),
    ("a-proposal-that-names-a-ref", _rewrite("refs/heads/proposed/0000000000a9", "ref: refs/heads/main\n"),
     ["symbolic-ref: refs/heads/proposed/0000000000a9"]),
    ("main-names-a-ref", _rewrite("refs/heads/main", "ref: refs/heads/release\n"),
     ["symbolic-ref: refs/heads/main"]),
    ("a-packed-tag", _rewrite("packed-refs", "# pack-refs with: peeled fully-peeled sorted \n%s refs/tags/v1\n" % _ID),
     ["packed-ref: refs/tags/v1"]),
    ("a-packed-annotated-tag-with-its-peeled-line",          # the `^<id>` line belongs to the tag above it: no ref
     _rewrite("packed-refs", "# pack-refs with: peeled fully-peeled sorted \n%s refs/tags/v2\n^%s\n" % (_ID, _ID)),
     ["packed-ref: refs/tags/v2"]),
    ("a-packed-line-that-is-no-ref", _rewrite("packed-refs", SECRET + "\n"),
     ["unread: packed-refs (a line that is not a ref)"]),
    ("head-names-another-branch", _rewrite("HEAD", "ref: refs/heads/release\n"), ["head: HEAD"]),
    ("head-names-a-commit", _rewrite("HEAD", _ID + "\n"), ["head: HEAD"]),
    # git's own space is a blank, a tab, a newline, a carriage return — not a vertical tab: this HEAD names a ref
    # that cannot exist, where Python's split() reads `refs/heads/main`
    ("head-ends-in-a-vertical-tab", _rewrite("HEAD", "ref: refs/heads/main\x0b\n"), ["head: HEAD"]),
    ("a-play-key-with-another-value", _config("receive.denyDeletes", "false"), ["config-value: receive.denydeletes"]),
    ("a-play-key-set-a-second-time", _append("config", "[receive]\n\tdenyNonFastForwards = false\n"),
     ["config-value: receive.denynonfastforwards"]),
    ("the-repository-says-it-is-not-bare", _config("core.bare", "false"), ["config-value: core.bare"]),
    ("a-packed-tag-after-a-tab",                      # git takes a tab between the id and the name; so does this
     _rewrite("packed-refs", "# pack-refs with: peeled fully-peeled sorted \n%s\trefs/tags/v3\n" % _ID),
     ["packed-ref: refs/tags/v3"]),
    ("main-holds-something-that-is-no-object-name", _rewrite("refs/heads/main", " ref: refs/heads/release\n"),
     ["unread: refs/heads/main"]),
    ("a-note-that-is-a-directory", lambda bare: _put(bare, "ORIG_HEAD/update", SECRET), ["not-a-file: ORIG_HEAD"]),
    ("a-lock-file-beside-a-proposal", _rewrite("refs/heads/proposed/0000000000a9.lock", _ID + "\n"),
     ["ref: refs/heads/proposed/0000000000a9.lock"]),
    ("head-with-a-second-line", _rewrite("HEAD", "ref: refs/heads/main\nref: refs/heads/release\n"), ["head: HEAD"]),
    ("head-with-something-after-the-name", _rewrite("HEAD", "ref: refs/heads/main release\n"), ["head: HEAD"]),
    ("head-names-a-branch-whose-name-begins-with-main", _rewrite("HEAD", "ref: refs/heads/main2\n"), ["head: HEAD"]),
    ("an-allowed-name-on-a-directory", lambda bare: _put(bare, "objects/info/packs/x", SECRET),
     ["not-a-file: objects/info/packs"]),
    ("no-head", _remove("HEAD"), ["head: HEAD"]),                      # to git, this is then no repository at all
    ("a-key-of-a-section-the-play-writes-in", _config("receive.unpackLimit"), ["config-key: receive.unpacklimit"]),
    ("a-play-key-added-a-second-time", lambda bare: R.git(bare, "config", "--add", "receive.denyNonFastForwards",
                                                         SECRET), ["config-value: receive.denynonfastforwards"]),
    ("a-collection-key-turned-back", _config("gc.writeCommitGraph", "true"),
     ["config-value: gc.writecommitgraph"]),
    ("a-play-key-gone", lambda bare: R.git(bare, "config", "--unset-all", "repack.updateServerInfo"),
     ["config-value: repack.updateserverinfo"]),
    ("the-hook-with-one-line-more", _append("hooks/update", "# %s\n" % SECRET), ["hook-changed: update"]),
    ("no-hooks-directory", lambda bare: shutil.rmtree(os.path.join(bare, "hooks")), ["hook-missing: update"]),
    # what git's own collection writes when nobody tells it not to (the play does): here, somebody's
    ("a-commit-graph", _rewrite("objects/info/commit-graph", SECRET), ["objects-info-entry: commit-graph"]),
    ("a-list-of-refs-in-info", _rewrite("info/refs", "%s\trefs/heads/%s\n" % (_ID, SECRET)), ["info-entry: refs"]),
    ("a-reflog-of-a-proposal-nobody-minted", _rewrite("logs/refs/heads/proposed/next", SECRET + "\n"),
     ["reflog: logs/refs/heads/proposed/next"]),
    ("a-branch-whose-name-begins-with-main", _rewrite("refs/heads/mainline", _ID + "\n"),
     ["ref: refs/heads/mainline"]),
    ("a-proposal-that-names-a-ref-with-no-space",            # git takes `ref:<name>` as it takes `ref: <name>`
     _rewrite("refs/heads/proposed/0000000000a9", "ref:refs/heads/main\n"),
     ["symbolic-ref: refs/heads/proposed/0000000000a9"]),
    ("a-ref-that-is-not-ours-and-names-a-ref", _rewrite("refs/heads/release", "ref: refs/heads/main\n"),
     ["ref: refs/heads/release", "symbolic-ref: refs/heads/release"]),
    ("a-ref-file-longer-than-a-ref", _rewrite("refs/heads/proposed/0000000000a9", SECRET * 20 + "\n"),
     ["unread: refs/heads/proposed/0000000000a9"]),
    ("a-peeled-line-under-no-ref",                          # git refuses the whole file for this one
     _rewrite("packed-refs", "# pack-refs with: peeled fully-peeled sorted \n^%s\n" % _ID),
     ["unread: packed-refs (a line that is not a ref)"]),
    ("a-peeled-line-left-under-one-of-the-tools-refs",       # a tag's line removed by hand, its `^` line not
     _rewrite("packed-refs", "# pack-refs with: peeled fully-peeled sorted \n%s refs/heads/main\n^%s\n" % (_ID, _ID)),
     ["unread: packed-refs (a line that is not a ref)"]),
    ("a-second-peeled-line-under-a-tag",                    # one `^` line belongs to a tag; git refuses a second
     _rewrite("packed-refs", "# pack-refs with: peeled fully-peeled sorted \n%s refs/tags/v4\n^%s\n^%s\n"
              % (_ID, _ID, _ID)),
     ["packed-ref: refs/tags/v4", "unread: packed-refs (a line that is not a ref)"]),
    ("two-packed-lines-that-are-no-refs", _rewrite("packed-refs", SECRET + "\n" + SECRET + "-again\n"),
     ["unread: packed-refs (a line that is not a ref)"]),
    # git takes ONE line that begins with `#`: its own header, first. Any other one it refuses the file for
    ("a-comment-line-below-the-first", _rewrite(
        "packed-refs", "# pack-refs with: peeled fully-peeled sorted \n%s refs/heads/main\n# %s\n" % (_ID, SECRET)),
     ["unread: packed-refs (a line that is not a ref)"]),
    ("a-first-line-that-is-not-gits-header", _rewrite("packed-refs", "# %s\n%s refs/heads/main\n" % (SECRET, _ID)),
     ["unread: packed-refs (a line that is not a ref)"]),
    ("an-empty-line-between-two-refs", _rewrite(
        "packed-refs", "# pack-refs with: peeled fully-peeled sorted \n%s refs/heads/main\n\n%s refs/heads/main\n"
        % (_ID, _ID)), ["unread: packed-refs (a line that is not a ref)"]),
]


@pytest.mark.parametrize("plant, expect", [(p, e) for _n, p, e in _PLANTED], ids=[n for n, _p, _e in _PLANTED])
def test_each_planted_thing_is_named_by_kind_and_key_and_never_by_value(tmp_path, capsys, plant, expect):
    """One thing is put into a canonical the play made — a config key that names a program, a key of a section
    the play itself writes in, one of the play's own keys turned, doubled or gone, an include, a second hook, a
    changed or missing `update` hook, a graft file, an alternates file, a commit-graph and the list of refs a
    collection of git's own would leave, a `commondir`, a `shallow` file, a reflog of a ref the tool does not
    write, a ref of that kind, a ref that names another ref, a line of `packed-refs` git would not take, a
    `HEAD` that does not name `main` or is gone — and the audit prints exactly the line that names it, its kind
    and its key; the VALUE it was given is nowhere in the output; and `--strict` exits 3 where the plain run
    exits 0. An include is NAMED and not FOLLOWED: the keys of the file it names are not in the report (the
    case whose included file is there shows it — asked of its standard input, git follows an include unless
    it is told not to, and would open whatever the line names).

    Guards both halves of what the report is for. Each of these changes what a git run in the repository
    does, and none of them shows in the Pending panel or in a promote; unnamed, a node carries it for good.
    And the output goes into a deploy's log, which more people read than may read the canonical: a key's
    value can be a path, a URL with a credential in it, a command line."""
    r = _as_the_play_makes_it(tmp_path)
    plant(r.bare)
    rc, lines = _report(r.bare, capsys=capsys)
    assert (rc, lines) == (0, expect)
    assert SECRET not in "\n".join(lines)
    assert _report(r.bare, "--strict", capsys=capsys) == (3, expect)


def test_what_git_leaves_by_itself_is_named_and_is_not_a_finding(tmp_path, capsys):
    """`FETCH_HEAD`, `ORIG_HEAD` and `gc.log` in the top directory are printed as notes, `objects/info/packs` is
    allowed outright, and with only them there `--strict` still exits 0.

    Guards the difference between "somebody put this here" and "git did": a strict audit that failed on a
    file an ordinary fetch leaves would be switched off the first day. The list is short on purpose. What a
    collection would write into `info/` and `objects/info/` is NOT on it: the play tells git not to write
    those (the clean row collects, and stays clean), so one that is there is somebody's — and a commit-graph
    is a cache git answers questions about history from."""
    r = _as_the_play_makes_it(tmp_path)
    for name in ("FETCH_HEAD", "ORIG_HEAD", "gc.log", "objects/info/packs"):
        _put(r.bare, name)
    assert _report(r.bare, "--strict", capsys=capsys) == (0, ["note: FETCH_HEAD", "note: ORIG_HEAD", "note: gc.log"])


def test_a_name_that_is_not_plain_text_is_printed_escaped_on_one_line_and_cut(tmp_path, capsys):
    """Refs listed in `packed-refs` whose NAMES hold an escape character, a carriage return followed by the
    text of another finding, a direction override (U+202E), a no-break space, bytes that are not UTF-8, and
    three hundred letters: each is ONE line of the report that begins with its own kind, with the odd bytes
    spelled out, and the long one cut and marked as cut. Where the file system takes such names, a ref FILE
    whose name holds a newline followed by the text of another finding, and a top-level entry of that kind,
    are one line each too. A thousand refs make `_MAX_PER_KIND` lines and one that counts the rest — and the
    one line of ANOTHER kind (a config key) is still in the report. A name that holds a backslash and the
    letter n is printed with the backslash doubled.

    Guards the log the report is printed into. A name is whatever somebody wrote; printed raw, an escape
    sequence repaints a terminal, a carriage return or a newline makes a forged line that reads as another
    finding — or hides the real one — and a direction override shows a name that is not the name. The cut is
    per kind because the report is sorted: with one cut for the whole report, four hundred empty files in
    `branches/` (the kind that sorts first) left not one other line in it — not the config key that names a
    hooks directory, not the changed hook. And the backslash is doubled always because otherwise a name with
    a newline in it and a name with a backslash and an n in it are the same line."""
    r = _as_the_play_makes_it(tmp_path)
    odd = (b"refs/tags/a\x1b[2Jb", b"refs/tags/c\rhook: forged", b"refs/tags/d\xe2\x80\xaee",
           b"refs/tags/f\xc2\xa0g", b"refs/tags/\xff\xfe", b"refs/tags/" + b"z" * 300)
    with open(os.path.join(r.bare, "packed-refs"), "wb") as fh:
        fh.write(b"# pack-refs with: peeled fully-peeled sorted \n")
        for name in odd:
            fh.write(_ID.encode() + b" " + name + b"\n")
    rc, lines = _report(r.bare, capsys=capsys)
    assert rc == 0 and len(lines) == len(odd) and all(line.startswith("packed-ref: refs/tags/") for line in lines)
    text = "\n".join(lines)
    assert not [ch for ch in text if ch != "\n" and not ch.isprintable()], "something that does not print got out"
    for spelled in ("a\\x1b[2Jb", "c\\rhook: forged", "d\\u202ee", "f\\u00a0g", "\\xff\\xfe"):
        assert spelled in text, spelled
    cut = [line for line in lines if "zzzz" in line]
    assert len(cut) == 1 and cut[0].endswith(chr(0x2026)) and len(cut[0]) < 300
    assert max(len(line) for line in lines) < 300

    os.unlink(os.path.join(r.bare, "packed-refs"))
    try:                                                    # names no Windows file system takes
        _put(r.bare, "refs/heads/a\nhook: forged", _ID + "\n")
        _put(r.bare, "x\nnote: gc.log")
    except OSError:
        pass
    else:
        assert _report(r.bare, capsys=capsys)[1] == ["ref: refs/heads/a\\nhook: forged", "top-entry: x\\nnote: gc.log"]
        os.unlink(os.path.join(r.bare, "refs", "heads", "a\nhook: forged"))
        os.unlink(os.path.join(r.bare, "x\nnote: gc.log"))

    with open(os.path.join(r.bare, "packed-refs"), "wb") as fh:
        for i in range(1000):
            fh.write(_ID.encode() + b" refs/tags/t%04d\n" % i)
    R.git(r.bare, "config", "core.hooksPath", SECRET)
    rc, lines = _report(r.bare, capsys=capsys)
    assert len([ln for ln in lines if ln.startswith("packed-ref: ")]) == tool._MAX_PER_KIND
    assert "unread: %d more `packed-ref` lines are not shown" % (1000 - tool._MAX_PER_KIND) in lines
    assert "config-key: core.hookspath" in lines
    with open(os.path.join(r.bare, "packed-refs"), "wb") as fh:
        fh.write(_ID.encode() + b" refs/tags/a\\nb\n")
    assert "packed-ref: refs/tags/a\\\\nb" in _report(r.bare, capsys=capsys)[1]


def test_no_link_is_followed_and_nothing_that_is_not_a_file_is_opened(tmp_path, capsys):
    """With `config` replaced by a symbolic link to a file that holds a key, a ref replaced by a link, and —
    where the host can make one — `HEAD` replaced by a named pipe: the audit names each (`link`, `not-a-file`),
    reports the config as unread rather than the keys behind the link, and RETURNS.

    Guards the reader against the directory it reads. A link is how a writer of the canonical points a root
    reader at any file of the machine, and a pipe where a file is expected is a reader that waits forever — in
    the middle of a deploy."""
    r = _as_the_play_makes_it(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("[core]\n\thooksPath = /x\n", encoding="utf-8")
    try:
        os.unlink(os.path.join(r.bare, "config"))
        os.symlink(str(elsewhere), os.path.join(r.bare, "config"))
        os.unlink(os.path.join(r.bare, "refs", "heads", "main"))
        os.symlink(str(elsewhere), os.path.join(r.bare, "refs", "heads", "main"))
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot make a symbolic link here")
    expect = ["config-unread: config", "link: config", "link: refs/heads/main"]
    if hasattr(os, "mkfifo"):
        os.unlink(os.path.join(r.bare, "HEAD"))
        os.mkfifo(os.path.join(r.bare, "HEAD"))
        expect.append("not-a-file: HEAD")
    rc, lines = _within(10, lambda: _report(r.bare, capsys=capsys))
    assert (rc, lines) == (0, sorted(expect))
    assert "hookspath" not in "\n".join(lines)


# Everything the script may reach for, by name. A CLOSED list: a way to start a process that nobody thought of
# (`os.posix_spawn`, `os.exec*`, `pty`, `multiprocessing`, a name imported under another name) is not in it.
_IMPORTS = {"argparse", "hashlib", "itertools", "os", "re", "stat", "subprocess", "sys"}
_FROM_IMPORTS = {("kontroll", "canon")}
_REACHED = {
    "os": {"path", "open", "read", "close", "fstat", "lstat", "stat", "scandir", "supports_dir_fd", "supports_fd",
           "environ", "devnull", "sep", "fsencode", "fsdecode", "geteuid", "O_RDONLY", "O_DIRECTORY", "O_NOFOLLOW"},
    "subprocess": {"run", "SubprocessError"},
    "sys": {"path", "stdout", "stderr", "exit", "argv"},
    "canon": {"show", "identity_kwargs", "Unreadable", "MAIN", "PROPOSED", "valid_run_id", "SERVICE_UID",
              "SERVICE_GID"},
}
# what is asked of `os` by NAME — `hasattr(os, …)`, `getattr(os, …, 0)`: flags and one question, nothing callable
_ASKED_OF_OS = {"O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC", "O_BINARY", "geteuid"}
# the builtin `open`: the checkout's own hook, and the place nothing is — never a path of the repository
_OPENED = ["SHIPPED_HOOK", "os.devnull"]
_NEVER = {"__import__", "eval", "exec", "compile", "globals", "locals", "vars", "importlib", "breakpoint",
          "setattr", "delattr", "input"}


def test_the_one_process_is_git_handed_the_config_as_bytes_and_never_the_repository(tmp_path, monkeypatch, capsys):
    """The script can start a process in ONE way, and it is written once. Read as a syntax tree: it imports a
    closed list of standard modules and `kontroll.canon` and nothing else, under their own names; of `os`,
    `subprocess`, `sys` and `canon` it reaches only for the names of a closed list (so no `os.posix_spawn`,
    `os.system`, `os.exec*`, `subprocess.Popen`, no `canon` reader that would open the repository), never
    holds one of those modules as a value (asking `os` whether it HAS a name, or for a flag with a default of
    0, is the one exception, by a closed list too), calls none of `eval`, `exec`, `__import__`, and opens with
    the builtin `open` only the checkout's own hook and the null device; `subprocess.run` is named once, in
    one call, with the literal `git config --file - --no-includes --list -z` and exactly these keywords: the
    config's bytes as `input`, captured output, a time limit, an environment, the file system's root as
    working directory, and `**canon.identity_kwargs(...)`. Run, that process is handed neither the
    repository's path nor anything below it; its environment holds what locates programs and temporary files,
    the four `GIT_*` entries that switch every other config scope and every prompt off, and nothing else — no
    `HOME`; its time limit is a number. When it cannot be started, when it does not end in time, and when a
    root caller cannot become the service, the report says the config was not read.

    Guards "it reads the repository as files". A git that is told where the repository is opens it — its
    config in force, its hooks one option away — and this audit is run exactly where that is not safe to
    assume; a second process, by any road, is one nobody has read. The list is closed on purpose: an open one
    (the names of the calls somebody thought of) passes the first road nobody thought of. And an audit that
    cannot list the keys must say so: silence would read as "no key of note"."""
    tree = ast.parse(open(SCRIPT, encoding="utf-8").read())
    nodes = list(ast.walk(tree))
    assert {a.name for n in nodes if isinstance(n, ast.Import) for a in n.names} == _IMPORTS
    assert not [a for n in nodes if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names if a.asname]
    assert {(n.module, a.name) for n in nodes if isinstance(n, ast.ImportFrom) for a in n.names} == _FROM_IMPORTS
    bases = set()
    for n in nodes:
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in _REACHED:
            assert n.attr in _REACHED[n.value.id], "%s.%s" % (n.value.id, n.attr)
            bases.add(id(n.value))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("getattr", "hasattr"):
            shape = len(n.args) == 2 if n.func.id == "hasattr" else len(n.args) == 3 and ast.unparse(n.args[2]) == "0"
            assert (shape and not n.keywords and isinstance(n.args[0], ast.Name) and n.args[0].id == "os"
                    and isinstance(n.args[1], ast.Constant) and n.args[1].value in _ASKED_OF_OS), ast.unparse(n)
            bases.add(id(n.args[0]))
    loose = [n for n in nodes if isinstance(n, ast.Name) and n.id in _REACHED and id(n) not in bases]
    assert not loose, "a module is held as a value: line %d" % loose[0].lineno
    assert not [n.id for n in nodes if isinstance(n, ast.Name) and n.id in _NEVER]
    opens = [n for n in nodes if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open"]
    assert sorted(ast.unparse(n.args[0]) for n in opens) == sorted(_OPENED)
    assert len([n for n in nodes if isinstance(n, ast.Name) and n.id == "open"]) == len(opens), "`open` held as a value"
    assert len([n for n in nodes if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id == "subprocess" and n.attr == "run"]) == 1
    starts = [n for n in nodes if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"]
    assert [n.func.attr for n in starts] == ["run"]
    call = starts[0]
    assert len(call.args) == 1
    assert ast.unparse(call.args[0]) == "['git', 'config', '--file', '-', '--no-includes', '--list', '-z']"
    keywords = {k.arg: ast.unparse(k.value) for k in call.keywords}
    assert set(keywords) == {"input", "capture_output", "timeout", "env", "cwd", None}
    assert keywords["input"] == "data" and keywords["cwd"] == "os.path.abspath(os.sep)" and keywords["env"] == "env"
    assert keywords["capture_output"] == "True"
    assert keywords["timeout"].isdigit() and int(keywords["timeout"]) <= 60
    assert keywords[None].startswith("canon.identity_kwargs(")

    r = _as_the_play_makes_it(tmp_path)
    seen = []
    real = subprocess.run

    def spy(argv, **kw):
        seen.append((list(argv), dict(kw)))
        return real(argv, **kw)
    monkeypatch.setattr(tool.subprocess, "run", spy)
    monkeypatch.setenv("HOME", str(tmp_path))                # so that "no HOME" is asked on every host
    assert _report(r.bare, capsys=capsys) == (0, [])
    (argv, kw), = seen
    inside = os.path.normcase(os.path.abspath(r.bare))
    handed = [str(a) for a in argv] + [str(kw["cwd"])] + ["%s=%s" % pair for pair in kw["env"].items()]
    assert not [h for h in handed if inside in os.path.normcase(h)], handed
    assert set(kw["env"]) - {"PATH", "SYSTEMROOT", "TEMP", "TMP"} == {
        "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT", "GIT_CEILING_DIRECTORIES"}
    assert (kw["env"]["GIT_CONFIG_NOSYSTEM"], kw["env"]["GIT_CONFIG_GLOBAL"], kw["env"]["GIT_TERMINAL_PROMPT"]) == (
        "1", os.devnull, "0")
    assert isinstance(kw["timeout"], (int, float)) and 0 < kw["timeout"] <= 60
    assert kw["input"] == open(os.path.join(r.bare, "config"), "rb").read()

    def absent(argv, **kw):
        raise FileNotFoundError("git")
    monkeypatch.setattr(tool.subprocess, "run", absent)
    assert _report(r.bare, capsys=capsys) == (0, ["config-unread: config"])

    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])
    monkeypatch.setattr(tool.subprocess, "run", slow)
    assert _report(r.bare, capsys=capsys) == (0, ["config-unread: config"])

    def refuses(argv, **kw):
        return subprocess.CompletedProcess(argv, 128, b"", b"fatal: bad config")
    monkeypatch.setattr(tool.subprocess, "run", refuses)
    assert _report(r.bare, capsys=capsys) == (0, ["config-unread: config"])
    monkeypatch.setattr(tool.subprocess, "run", real)

    def cannot(repo):
        raise canon.Unreadable("could not drop to the service uid; nothing was read or changed")
    monkeypatch.setattr(canon, "identity_kwargs", cannot)
    assert _report(r.bare, capsys=capsys) == (0, ["config-unread: config"])


def test_a_path_that_is_no_directory_is_a_usage_error_and_nothing_is_audited(tmp_path, capsys):
    """`--audit` of a path that is not there, and of a file, exits 2 with one sentence on stderr — one line, the
    path in it spelled out where it is not plain text — and prints no report; so does a call with no `--audit`
    at all.

    Guards a deploy reading "nothing to report" from an audit that looked at nothing (a mistyped path, a
    canonical that is not mounted) — and the sentence itself: it goes into the same log as the report, and the
    path in it is whatever the caller was handed."""
    for target in (str(tmp_path / "not-there"), __file__, str(tmp_path / "not\x1b[2J-there")):
        assert tool.main(["--audit", target]) == 2
        said = capsys.readouterr()
        assert said.out == "" and "nothing was audited" in said.err
        assert "\x1b" not in said.err and said.err.count("\n") == 1
    assert tool.main([]) == 2


def test_the_play_audits_before_it_writes_shows_the_report_and_never_stops_on_it():
    """`local-canonical.yml` audits a canonical that is already there BEFORE its first task that writes into
    one: a `stat` of the canonical's path that starts nothing on it; then — only when something is there — a
    command of `scripts/kontroll-canonical.py --audit {{ bare }}` in the worktree, read-only under `--check`
    too (`check_mode: false`), never "changed", never failing the play, as whoever runs the play; then two
    tasks whose conditions and messages are EVALUATED here, with Jinja, against what the command can register.
    The report's lines are shown when there are any (a finding, or only a note) and not for a clean canonical.
    When the audit ended with anything but 0 — or registered no status at all — a sentence says that this was
    NO REPORT, followed by what the tool or the module said. Neither is shown when there was no canonical to
    audit. Every other task of the play that names the canonical stands after these four. And the play's own
    `git config` REPLACES every occurrence of a key.

    Guards the ways this could go wrong in a deploy: an audit that runs after the play has put the canonical
    right (it wrote `HEAD`, its keys and the hook first, so a deploy could never name one of them — the report
    was of what the play had just made); an audit that is skipped in the mandated dry-run; an audit that
    stops a deploy — whether a finding should ever stop one is a question nobody has answered; and an audit
    that did not run and says nothing, which reads exactly like a clean one."""
    import jinja2
    tasks = _play_tasks()
    at = next(i for i, t in enumerate(tasks) if "kontroll-canonical.py" in str(t.get("ansible.builtin.command", "")))
    before, audit, show, no_report = tasks[at - 1:at + 3]
    assert before["ansible.builtin.stat"] == {"path": "{{ bare }}", "get_checksum": False, "get_mime": False,
                                              "get_attributes": False}
    assert set(before) == {"name", "ansible.builtin.stat", "register"}
    assert audit["ansible.builtin.command"] == {
        "cmd": "python3 -B scripts/kontroll-canonical.py --audit {{ bare }}", "chdir": "{{ worktree }}"}
    assert audit["check_mode"] is False and audit["changed_when"] is False and audit["failed_when"] is False
    assert set(audit) == {"name", "ansible.builtin.command", "register", "changed_when", "failed_when", "check_mode",
                          "when"}
    assert audit["when"] == "%s.stat.exists" % before["register"]
    assert set(show) == {"name", "ansible.builtin.debug", "when"}
    assert set(no_report) == {"name", "ansible.builtin.debug", "when", "vars"}
    others = [i for i, t in enumerate(tasks) if not at - 1 <= i <= at + 2
              and "bare" in yaml.safe_dump({k: v for k, v in t.items() if k != "name"})]
    assert others and min(others) > at + 2, "a task that touches the canonical stands in front of its audit"
    assert tasks[min(others)]["name"].startswith("Create the bare-repo dir"), "control: that is the first of them"
    assert " config --replace-all {{ item.key }} {{ item.value }}" in _harden()["ansible.builtin.command"]["cmd"]

    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    reg, was = audit["register"], before["register"]

    def expression(text):
        text = text.strip()
        assert text.startswith("{{") and text.endswith("}}"), text
        return env.compile_expression(text[2:-2])
    shown, said = env.compile_expression(show["when"]), expression(show["ansible.builtin.debug"]["msg"])
    refused, why = env.compile_expression(no_report["when"]), expression(no_report["ansible.builtin.debug"]["msg"])
    (name, sentence), = no_report["vars"].items()

    def told(registered, canonical_there=True):
        """What the two tasks print for what the audit registered: (the report's lines, the "no report" lines)
        — None for a task that is not shown."""
        scope = {reg: registered, was: {"stat": {"exists": canonical_there}}, "bare": "/srv/kontroll.git",
                 "worktree": "/checkout"}
        scope[name] = env.from_string(sentence).render(**scope)
        return (said(**scope) if shown(**scope) else None, why(**scope) if refused(**scope) else None)
    assert told({"rc": 0, "stdout_lines": ["hook: post-receive"], "stderr_lines": []}) == (["hook: post-receive"], None)
    assert told({"rc": 0, "stdout_lines": ["note: gc.log"], "stderr_lines": []}) == (["note: gc.log"], None)
    assert told({"rc": 0, "stdout_lines": [], "stderr_lines": []}) == (None, None)
    refusal = "kontroll-canonical: /srv/kontroll.git is not a directory; nothing was audited"
    report, no = told({"rc": 2, "stdout_lines": [], "stderr_lines": [refusal]})
    assert report is None and len(no) == 2 and no[1] == refusal
    assert "NO REPORT of /srv/kontroll.git (exit status 2)" in no[0] and "in /checkout" in no[0]
    own = "unread: the audit ended on an error of its own (MemoryError); this is not a report of the canonical"
    report, no = told({"rc": 1, "stdout_lines": [own], "stderr_lines": []})
    assert report == [own] and len(no) == 1 and "(exit status 1)" in no[0]
    report, no = told({"msg": "Unable to change directory before execution", "stdout_lines": [], "stderr_lines": []})
    assert report is None and "(exit status none)" in no[0] and no[1] == "Unable to change directory before execution"
    skipped = {"changed": False, "skipped": True, "skip_reason": "Conditional result was False"}
    assert told(skipped, canonical_there=False) == (None, None)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a named pipe and a symbolic link")
def test_the_reader_of_small_files_opens_a_regular_file_and_nothing_else(tmp_path):
    """`_Dir`, the one class through which the audit opens anything of the repository. `read`: a regular file
    is its bytes and its `stat`; the same file through a symbolic link is nothing; a named pipe is nothing, at
    once; a directory, a file larger than the limit, a name that is not there are nothing. `sub`: a directory
    is opened; a link to one, a file, a name that is not there are not. `kind` says what each IS.

    Guards the reader by itself. The audit looks at what each entry IS before it reads it, so a link or a pipe
    is normally named and never opened — but between that look and the open the entry can be exchanged by
    whoever writes the directory, and then the open is the only check there is — for the entry itself; a
    directory above it is opened from its parent and not looked up again."""
    plain = tmp_path / "plain"
    plain.write_bytes(b"abc")
    os.symlink(str(plain), str(tmp_path / "link"))
    os.mkfifo(str(tmp_path / "pipe"))
    os.mkdir(str(tmp_path / "sub"))
    os.symlink(str(tmp_path / "sub"), str(tmp_path / "sublink"))
    with tool._Dir.top(os.fsencode(str(tmp_path))) as here:
        assert here.read(b"plain", 10)[0] == b"abc" and here.read(b"plain", 3)[0] == b"abc"
        assert stat.S_ISREG(here.read(b"plain", 3)[1].st_mode)
        assert here.read(b"plain", 2) == (None, None)
        assert tool._REF_BYTES <= 1024 and tool._PACKED_BYTES <= 64 << 20 and tool._HOOK_BYTES <= 8 << 20, (
            "what the audit reads of one file is small: the file is somebody else's to make as large as they like")
        assert here.read(b"link", 10) == (None, None)
        assert _within(10, lambda: here.read(b"pipe", 10)) == (None, None)
        assert here.read(b"sub", 10) == (None, None)
        assert here.read(b"not-there", 10) == (None, None)
        assert here.sub(b"sublink") is None and here.sub(b"plain") is None and here.sub(b"not-there") is None
        with here.sub(b"sub") as below:
            assert below.names() == ([], False)
        assert [here.kind(n) for n in (b"plain", b"link", b"pipe", b"sub", b"sublink", b"not-there")] == [
            "file", "link", "other", "dir", "link", None]


def test_a_config_that_would_make_git_print_far_more_than_it_holds_is_not_handed_to_git(tmp_path, monkeypatch, capsys):
    """A `config` of one section header 4 000 bytes long and a thousand two-byte keys, and one larger than
    `_CONFIG_BYTES`: the report says `config-unread`, and git was not started for either.

    Guards the memory of the process that runs the audit — root, on the control node, in a deploy. git prints
    `<section>.<name>` for every key, so the size of its answer is the header's length TIMES the number of
    keys: 220 kB of config gave 1.9 GB of output (measured), 700 kB would give 46 GB, and `subprocess.run`
    collects all of it. Whoever writes `config` — the service, when the deploy is armed — chooses both
    numbers."""
    r = _as_the_play_makes_it(tmp_path)
    started = []
    monkeypatch.setattr(tool.subprocess, "run", lambda *a, **k: started.append(a) or (_ for _ in ()).throw(OSError()))
    as_made = open(os.path.join(r.bare, "config"), encoding="utf-8").read()
    for body in ("[" + "a" * 4000 + "]\n" + "k\n" * 1000, "[x]\n" + "\tk = v\n" * (tool._CONFIG_BYTES // 6)):
        _put(r.bare, "config", as_made + body)
        assert _report(r.bare, capsys=capsys) == (0, ["config-unread: config"])
    assert started == []
    assert tool._CONFIG_BYTES * tool._CONFIG_LINE <= 1 << 26, "git's answer is bounded by about half this product"


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="needs a mode that says whether a file may be executed")
def test_an_update_hook_git_would_not_run_is_named(tmp_path, capsys):
    """`hooks/update` with the shipped bytes and no execute bit: the report says `hook-off: update` (and not
    `hook-changed`), and `--strict` exits 3; with the bit back, nothing is reported.

    Guards the one line of the report that vouches for something. git skips a hook that is not executable —
    it prints a hint to the PUSHER and takes the push — so a canonical whose `update` has the right bytes and
    mode 0644 has no allow-list at all, and an audit that compares bytes says nothing."""
    r = _as_the_play_makes_it(tmp_path)
    os.chmod(os.path.join(r.bare, "hooks", "update"), 0o644)
    assert _report(r.bare, "--strict", capsys=capsys) == (3, ["hook-off: update"])
    os.chmod(os.path.join(r.bare, "hooks", "update"), 0o755)
    assert _report(r.bare, "--strict", capsys=capsys) == (0, [])


_LISTED = ("info", "branches", "hooks", "refs", "refs/heads", "logs", "objects", "objects/info")
# (the entry the audit has just looked at, the directory that is exchanged at that moment)
_EXCHANGES = [(where, where) for where in _LISTED] + [
    ("refs/heads", "refs"), ("logs/refs", "logs"), ("objects/info", "objects"),      # a PARENT of what is opened next
    ("refs/heads/main", "refs/heads"), ("hooks/update", "hooks")]                    # … and of a FILE that is read next


@pytest.mark.skipif(not getattr(tool, "_AT", False), reason="needs directories opened from a descriptor")
@pytest.mark.parametrize("looked_at, exchanged", _EXCHANGES, ids=["%s--%s" % pair for pair in _EXCHANGES])
def test_a_directory_exchanged_for_a_link_after_it_was_looked_at_is_not_listed(tmp_path, monkeypatch, capsys,
                                                                                 looked_at, exchanged):
    """A directory of the repository is exchanged for a symbolic link to a directory OUTSIDE it at the worst
    moment — right after the audit has looked at an entry and before it opens it. Either the directory it is
    about to open is the one exchanged (each of the eight it lists), or a PARENT of it is (`refs` while it
    stands in `refs` and is about to open `heads`; `refs/heads` and `hooks` while it is about to read `main`
    and `update`). The report holds no name of the outside directory and nothing that only an outside file's
    content explains; an exchanged directory is said to be unread (or a link); and with a parent exchanged the
    report is that of the repository — nothing, here. (Outside, `main` and `update` are directories where the
    repository has files: a LOOK that went outside would say so, and the report would not be empty.)

    Guards "it reads the repository" against whoever writes the repository while it is read. With paths, the
    look (`lstat`) and the listing (`listdir`) each resolve the name again, and a link in ANY component is
    followed — `O_NOFOLLOW` covers the last one only: a writer that exchanges a directory in a loop had
    another directory's names in the report — as root, a listing of any directory of the machine in a
    deploy's log — in a quarter to a half of the runs (measured, 3 000 rounds a directory), and files there
    opened and read. Opened from the parent's descriptor with `O_NOFOLLOW`, an exchanged name is refused, and
    a parent that is exchanged is no longer on the way. The exchange is made by wrapping the look itself, so
    the row does not depend on winning a race."""
    r = _as_the_play_makes_it(tmp_path)
    for made in ("logs/refs/heads", "branches"):
        os.makedirs(os.path.join(r.bare, *made.split("/")), exist_ok=True)
    outside = tmp_path / "outside"
    for sub in ("", "heads", "info", "refs/heads"):
        os.makedirs(str(outside / sub), exist_ok=True)
        for name in ("OUTSIDE-name", "exclude", "packs", "HEAD"):
            (outside / sub / name).write_text("ref: refs/heads/OUTSIDE-content\n", encoding="utf-8")
        for name in ("main", "update"):              # of another KIND than their namesakes inside (files there)
            os.makedirs(str(outside / sub / name))
    parent, _, leaf = looked_at.rpartition("/")
    standing_in = os.path.normpath(os.path.join(r.bare, *parent.split("/")) if parent else r.bare)
    target = os.path.join(r.bare, *exchanged.split("/"))
    real_kind, done = tool._Dir.kind, []

    def kind_then_exchange(self, name):
        answer = real_kind(self, name)
        if not done and answer and name == leaf.encode() and os.path.normpath(os.fsdecode(self.path)) == standing_in:
            done.append(1)
            os.rename(target, target + ".was")
            os.symlink(str(outside), target)
        return answer
    monkeypatch.setattr(tool._Dir, "kind", kind_then_exchange)
    rc, lines = _report(r.bare, capsys=capsys)
    assert done, "control: the exchange was made"
    text = "\n".join(lines)
    assert rc == 0 and "OUTSIDE" not in text and "symbolic-ref" not in text and "hook-changed" not in text
    if looked_at == exchanged:
        assert ("unread: %s" % exchanged) in lines or ("link: %s" % exchanged) in lines
    else:
        assert lines == []


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root may look at everything")
def test_a_part_the_caller_may_not_look_at_is_said_and_not_passed_over(tmp_path, capsys):
    """A directory under `refs/heads/` that the caller may list and not search (mode 0400 on it — a name is
    seen and nothing can be asked about it), holding a ref: the report says `unread` for what is in it.

    Guards the sentence "every part that cannot be read is itself a line of the report". An entry that could
    not be looked at was taken for one that is not there: for an operator's own run, a directory the service
    made with the group's search bit off hid every ref in it (measured: the operator's audit printed nothing
    where root's printed two refs) — while a git run by the service read them all."""
    r = _as_the_play_makes_it(tmp_path)
    _put(r.bare, "refs/heads/x/evil", _ID + "\n")
    os.chmod(os.path.join(r.bare, "refs", "heads", "x"), 0o400)
    try:
        assert _report(r.bare, "--strict", capsys=capsys) == (3, ["unread: refs/heads/x/evil"])
    finally:
        os.chmod(os.path.join(r.bare, "refs", "heads", "x"), 0o700)


def test_a_tree_deeper_than_a_ref_can_be_is_said_and_not_walked(tmp_path, capsys):
    """Forty directories one inside the other under `refs/`, with a file at the bottom: the report says `refs`
    is deeper than the audit reads, and returns.

    Guards two things. The walk opens one directory per level, so its depth is what bounds the descriptors it
    holds. And a tree deeper than a path can be long ended the old walk in SILENCE — five thousand levels
    printed nothing at all (measured), because an entry whose path could not be looked at was taken for one
    that is not there."""
    r = _as_the_play_makes_it(tmp_path)
    _put(r.bare, "refs/" + "/".join(["d"] * 40) + "/leaf", _ID + "\n")
    assert _report(r.bare, capsys=capsys) == (0, ["unread: refs (deeper than this reads)"])


def _within(seconds, call):
    """`call()`'s result — or an AssertionError when it has not returned after `seconds`. For the rows whose
    fault is a reader that WAITS: without it the row would hang the suite instead of failing (a named pipe
    opened for reading with nobody writing never returns)."""
    box = []
    worker = threading.Thread(target=lambda: box.append(call()), daemon=True)
    worker.start()
    worker.join(seconds)
    assert box, "the call did not return within %d s: it is waiting on something it opened" % seconds
    return box[0]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a named pipe")
def test_a_pipe_where_a_file_is_read_fails_the_row_instead_of_hanging_it(tmp_path, capsys):
    """With a named pipe at `HEAD`, at `packed-refs`, at a ref and at `hooks/update`, the audit returns within
    ten seconds (it is run in a thread that is given that long) and names each.

    Guards the suite as much as the audit: a reader that opens a pipe and waits is the fault these rows are
    about, and a row that shows it by never returning is killed by whoever runs the suite — which is not a
    verdict. (Found by taking `O_NONBLOCK` out of the reader: the suite stood still.)"""
    r = _as_the_play_makes_it(tmp_path)
    for rel in ("HEAD", "packed-refs", "refs/heads/main", "hooks/update"):
        path = os.path.join(r.bare, *rel.split("/"))
        if os.path.lexists(path):
            os.unlink(path)
        os.mkfifo(path)
    rc, lines = _within(10, lambda: _report(r.bare, capsys=capsys))
    assert rc == 0 and {"not-a-file: HEAD", "not-a-file: packed-refs", "not-a-file: refs/heads/main",
                        "not-a-file: hooks/update", "hook-changed: update"} <= set(lines)


def test_a_link_is_named_where_it_stands_and_is_never_gone_into(tmp_path, capsys):
    """`description` as a symbolic link; `hooks/update` as a link to the very file the checkout ships (the same
    bytes, read through a link); a directory under `refs/` exchanged for a link to a directory that holds a
    file; `info/exclude`, an entry the audit allows BY NAME, as a link: each is a `link` line, the hook is
    `hook-changed` although its bytes would compare equal, and the file behind the linked directory is not a
    line.

    Guards "following no link" in the places the first row of that name does not stand: a top-level entry
    that is only looked at, a file whose CONTENT is the question (equal bytes through a link are somebody
    else's file tomorrow), a directory of the walk (a link that is gone into lists another directory under
    the repository's names), and a name on an allow-list — the right name on a thing that is not a file is
    not what the list allows."""
    r = _as_the_play_makes_it(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "inside").write_text(_ID + "\n", encoding="utf-8")
    try:
        os.unlink(os.path.join(r.bare, "description"))
        os.symlink(str(elsewhere / "inside"), os.path.join(r.bare, "description"))
        os.unlink(os.path.join(r.bare, "hooks", "update"))
        os.symlink(HOOK, os.path.join(r.bare, "hooks", "update"))
        os.symlink(str(elsewhere), os.path.join(r.bare, "refs", "remotes"))
        os.unlink(os.path.join(r.bare, "info", "exclude"))
        os.symlink(str(elsewhere / "inside"), os.path.join(r.bare, "info", "exclude"))
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot make a symbolic link here")
    assert _report(r.bare, capsys=capsys) == (0, ["hook-changed: update", "link: description", "link: hooks/update",
                                                  "link: info/exclude", "link: refs/remotes"])


def test_a_name_the_output_cannot_encode_is_printed_escaped_and_the_command_still_ends_in_its_report(tmp_path):
    """Run as a PROCESS with a standard output that takes only ASCII (`PYTHONIOENCODING=ascii`), on a canonical
    with a ref whose name holds a printable letter that is not ASCII and one long enough to be cut: exit 0,
    nothing on stderr, and both lines arrive in ASCII — the letter and the mark of the cut each spelled as a
    CHARACTER (a backslash, `u` and four digits), never as a byte (a backslash, `x` and two).

    Guards the last step of the report, which the rows above cannot see: they read what the script handed to
    `print`, and a deploy reads what came out of the pipe. A printable name is printed as it is; where the
    deploy's output is not UTF-8 (a C locale) that print raises, and without the fallback the operator reads
    a traceback where the finding was — the mark of a cut name is itself outside ASCII, so every long name
    would do it. And the spelling matters: in a name, a backslash, `x` and two digits is how a byte that is
    not UTF-8 is shown, and a letter must not read like one."""
    r = _as_the_play_makes_it(tmp_path)
    with open(os.path.join(r.bare, "packed-refs"), "wb") as fh:
        fh.write(_ID.encode() + b" refs/tags/caf\xc3\xa9\n" + _ID.encode() + b" refs/tags/" + b"z" * 300 + b"\n")
    env = dict(os.environ, PYTHONIOENCODING="ascii")
    p = subprocess.run([sys.executable, "-B", SCRIPT, "--audit", r.bare], capture_output=True, env=env, timeout=120)
    assert (p.returncode, p.stderr) == (0, b"")
    lines = p.stdout.decode("ascii").splitlines()
    assert len(lines) == 2 and lines[0] == "packed-ref: refs/tags/caf\\u00e9", lines
    assert lines[1].startswith("packed-ref: refs/tags/zzz") and lines[1].endswith("z\\u2026")
    assert lines[1].startswith("packed-ref: refs/tags/zzz") and lines[1].endswith("z\\u2026")


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root may read everything")
def test_what_the_caller_may_not_read_is_each_a_line(tmp_path, capsys):
    """With `packed-refs`, one proposal's ref file and `hooks/` each made unreadable to the caller (mode 000):
    the report says `unread` for each of the three (and, for `hooks/`, that no `update` hook was found).

    Guards the audit run by an operator, who — unlike root — can be refused. Each of these three was a
    separate `if … is None` whose line could be dropped without a row noticing."""
    r = _as_the_play_makes_it(tmp_path)
    _put(r.bare, "packed-refs", "# pack-refs with: peeled fully-peeled sorted \n")
    locked = [os.path.join(r.bare, "packed-refs"), os.path.join(r.bare, "refs", "heads", "proposed", R.FF),
              os.path.join(r.bare, "hooks")]
    for path in locked:
        os.chmod(path, 0)
    try:
        assert _report(r.bare, capsys=capsys) == (0, ["hook-missing: update", "unread: hooks", "unread: packed-refs",
                                                      "unread: refs/heads/proposed/" + R.FF])
    finally:
        for path in locked:
            os.chmod(path, 0o700)


def test_a_link_in_the_object_store_is_named(tmp_path, capsys):
    """A symbolic link in `objects/` (one of its two-digit directories exchanged for a link) and one in
    `objects/pack/`: each is a `link` line; a plain stray file beside them is not a line.

    Guards the one thing of the object store this audit does say. Through such a link git reads objects from
    another directory — what `objects/info/alternates` does, which is named — and the tool's own check that
    refuses a repository with a link (`canon.git_kwargs`) asks about `objects`, not about what is in it."""
    r = _as_the_play_makes_it(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.makedirs(os.path.join(r.bare, "objects", "pack"), exist_ok=True)
    try:
        os.symlink(str(elsewhere), os.path.join(r.bare, "objects", "zz"))
        os.symlink(str(elsewhere), os.path.join(r.bare, "objects", "pack", "pack-0000.pack"))
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot make a symbolic link here")
    _put(r.bare, "objects/stray")
    assert _report(r.bare, "--strict", capsys=capsys) == (3, ["link: objects/pack/pack-0000.pack", "link: objects/zz"])


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == canon.SERVICE_UID,
                    reason="needs modes, and a caller who is not the service uid")
def test_a_hooks_directory_the_service_could_write_is_named(tmp_path, capsys):
    """`hooks/` writable by everybody, then `hooks/update` writable by everybody: `hook-writable: hooks`, then
    `hook-writable: update` as well — with the hook's bytes and its execute bit as the play leaves them.

    Guards the grant the play ends on (`hooks/` root's, `0755`, recursively). The hook's bytes say what the
    hook does today; who may write the file and its directory says what it does tomorrow, and the report is
    read once per deploy."""
    r = _as_the_play_makes_it(tmp_path)
    os.chmod(os.path.join(r.bare, "hooks"), 0o777)
    assert _report(r.bare, capsys=capsys) == (0, ["hook-writable: hooks"])
    os.chmod(os.path.join(r.bare, "hooks", "update"), 0o757)
    assert _report(r.bare, "--strict", capsys=capsys) == (3, ["hook-writable: hooks", "hook-writable: update"])


def test_a_directory_with_more_entries_than_the_audit_reads_says_so(tmp_path, monkeypatch, capsys):
    """With the limit set to five: eight files in `info/` give `unread: info (more entries than this reads)`
    beside the entries it did read, and eight files under `refs/tags/` give the same for `refs`.

    Guards the bound on what a directory can make the audit hold — and that reaching it is SAID: a listing
    that stops early and reports the part it saw would read as the whole."""
    r = _as_the_play_makes_it(tmp_path)
    monkeypatch.setattr(tool, "_MAX_ENTRIES", 5)
    for i in range(8):
        _put(r.bare, "info/extra%d" % i)
    rc, lines = _report(r.bare, capsys=capsys)
    assert "unread: info (more entries than this reads)" in lines
    assert len([ln for ln in lines if ln.startswith("info-entry: ")]) in (4, 5)
    for i in range(8):
        os.unlink(os.path.join(r.bare, "info", "extra%d" % i))
        _put(r.bare, "refs/tags/t%d" % i, _ID + "\n")
    assert "unread: refs (more entries than this reads)" in _report(r.bare, capsys=capsys)[1]


def test_an_error_of_the_audits_own_ends_in_a_line_of_the_report_and_exit_1(tmp_path, monkeypatch, capsys):
    """When `audit` itself raises, the command prints one `unread:` line that names the kind of error and says
    this is not a report of the canonical, and exits 1 — no traceback.

    Guards what the operator reads in a deploy. The play prints the audit's output and goes on; a traceback
    there is twenty lines that say nothing about the canonical, and an audit that died silently is an empty
    list, which reads as "nothing found"."""
    r = _as_the_play_makes_it(tmp_path)
    monkeypatch.setattr(tool, "audit", lambda repo: (_ for _ in ()).throw(MemoryError()))
    assert tool.main(["--audit", r.bare]) == 1
    said = capsys.readouterr()
    assert said.err == "" and said.out.splitlines() == [
        "unread: the audit ended on an error of its own (MemoryError); this is not a report of the canonical"]


def test_the_reader_of_small_files_takes_a_regular_file_whole_or_not_at_all(tmp_path):
    """`_Dir.read` on any host: a regular file is its bytes when it is no larger than the limit — at the limit
    exactly, too — and nothing when it is one byte larger; an empty file is empty bytes, not nothing; a name
    that is not there, and a directory, are nothing.

    Guards the half of the reader that needs no pipe and no link, on the hosts where the row beside this one is
    skipped. "Larger than this reads" has to be nothing and never the first part: a `packed-refs` or a `config`
    read up to a limit is a report on the part somebody chose to put first."""
    (tmp_path / "plain").write_bytes(b"abc")
    (tmp_path / "empty").write_bytes(b"")
    (tmp_path / "sub").mkdir()
    with tool._Dir.top(os.fsencode(str(tmp_path))) as here:
        assert here.read(b"plain", 10)[0] == b"abc" and here.read(b"plain", 3)[0] == b"abc"
        assert here.read(b"plain", 2) == (None, None)
        assert here.read(b"empty", 10)[0] == b""
        assert here.read(b"not-there", 10) == (None, None)
        assert here.read(b"sub", 10) == (None, None)


def test_a_part_that_cannot_be_listed_or_compared_is_a_line_whoever_asks(tmp_path, monkeypatch, capsys):
    """Each part the audit cannot take in is SAID: a directory that cannot be listed — the top one, `info/`,
    `hooks/`, one below `refs/` (a lister that fails for exactly that directory stands in for a mode the caller
    cannot pass, so the row holds for root too) — is `unread`; so is a `packed-refs` larger than the audit
    reads; and a checkout that has no shipped hook to compare the canonical's with reports the hook as changed.

    Guards the difference between "nothing there" and "not looked at". Every one of these is a place where the
    reader gives up on purpose — a size, a permission, a file of its own that is missing — and whoever writes
    the canonical chooses whether it does: a part that is passed over in silence is where the next thing is
    put. The rows that take a mode away are skipped for root, who may read everything; this one is not."""
    r = _as_the_play_makes_it(tmp_path)
    names = tool._Dir.names

    def blind_to(*parts):
        top = os.fsencode(os.path.abspath(r.bare))
        blind = {os.path.join(top, *os.fsencode(part).split(b"/")) if part else top for part in parts}
        monkeypatch.setattr(tool._Dir, "names",
                            lambda self, limit=None: None if self.path in blind else names(self, limit))
    blind_to("")
    assert _report(r.bare, "--strict", capsys=capsys) == (3, ["unread: the top directory"])
    blind_to("info", "refs/heads/proposed", "hooks")
    assert _report(r.bare, capsys=capsys) == (0, ["unread: hooks", "unread: info", "unread: refs/heads/proposed"])
    monkeypatch.setattr(tool._Dir, "names", names)

    _put(r.bare, "packed-refs", "%s refs/heads/main\n" % _ID)
    assert _report(r.bare, capsys=capsys) == (0, []), "control: this packed-refs is read, and is the tool's own"
    monkeypatch.setattr(tool, "_PACKED_BYTES", 16)
    assert _report(r.bare, capsys=capsys) == (0, ["unread: packed-refs"])
    os.unlink(os.path.join(r.bare, "packed-refs"))

    monkeypatch.setattr(tool, "SHIPPED_HOOK", str(tmp_path / "no-such-checkout" / "canonical-update-hook.sh"))
    assert _report(r.bare, capsys=capsys) == (0, ["hook-changed: update"])


def test_a_key_of_the_play_that_is_there_twice_is_named_and_the_plays_own_command_puts_it_right(tmp_path, capsys):
    """Each key of the play's loop is given a SECOND line in `config`, with another value: the audit names every
    one as `config-value`. Then the play's own command for each item, read from the play, runs without an
    error; the key is there once, with the play's value; and the audit prints nothing.

    Guards a deploy against one line. git will not set a key that is in the file twice with a single value (it
    exits 5 — the control here) unless it is told to replace them all. With the plain form, one more line in
    `config` — which whoever may write the canonical can add — stopped every deploy at the play's "Harden"
    task; and until somebody removed it by hand, what git had in force for that key was the LAST line, not
    the play's."""
    r = _as_the_play_makes_it(tmp_path)
    loop = _harden()["loop"]
    for item in loop:
        section, name = item["key"].split(".")
        _append("config", "[%s]\n\t%s = %s\n" % (section, name, "false" if item["value"] == "true" else "true"))(r.bare)
    assert _report(r.bare, capsys=capsys) == (0, sorted("config-value: " + item["key"].lower() for item in loop))
    assert R.rc(r.bare, "config", loop[0]["key"], loop[0]["value"]) != 0, "control: the plain form refuses it"
    for item in loop:
        _set_as_the_play_does(r.bare, item)
        assert R.git(r.bare, "config", "--get-all", item["key"]).split() == [item["value"].encode()]
    assert _report(r.bare, "--strict", capsys=capsys) == (0, [])


def test_every_kind_the_audit_prints_has_a_line_in_both_tables():
    """The kinds the script can print — the first argument of every `report.add(...)` and the kind handed to
    every `_only(...)` / `_only_in(...)`, read from its syntax tree — are exactly the kinds of the table in its
    own docstring, and exactly the kinds that open a row of the table in docs/troubleshooting.md ("The audit of
    the canonical named something").

    Guards the operator who reads a line in a deploy's output and looks it up. A kind added to the script and
    to neither table is a line nobody can act on; a row left in a table for a kind the script no longer prints
    is a promise the audit does not keep."""
    tree = ast.parse(open(SCRIPT, encoding="utf-8").read())
    printed = set()
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        if isinstance(n.func, ast.Attribute) and n.func.attr == "add" and ast.unparse(n.func.value) == "report":
            kind = n.args[0]
        elif isinstance(n.func, ast.Name) and n.func.id in ("_only", "_only_in"):
            kind = n.args[-1]
        else:
            continue
        if isinstance(kind, ast.Constant):
            printed.add(kind.value)
        else:
            assert ast.unparse(kind) == "kind", "a kind that is not written out: line %d" % n.lineno
    own = set(re.findall(r"^  ([a-z]+(?:-[a-z]+)*) {2,}\S", ast.get_docstring(tree, clean=False), re.M))
    doc = open(os.path.join(ROOT, "docs", "troubleshooting.md"), encoding="utf-8").read()
    section = doc[doc.index("## 8. The audit of the canonical named something"):].split("\n## ")[0]
    theirs = set(re.findall(r"`([a-z]+(?:-[a-z]+)*): ", section))
    assert len(printed) > 10, "control: the kinds were found in the script"
    assert printed == own, sorted(printed ^ own)
    assert printed == theirs, sorted(printed ^ theirs)


_VALUE_SPELLINGS = [
    ("sharedrepository-upper", "core.sharedRepository", " GROUP"),       # receive-pack rejects it (RAN-L(op))
    ("sharedrepository-quoted-space", "core.sharedRepository", ' " group"'),
    ("sharedrepository-mixed-case", "core.sharedRepository", " Group"),
    ("denydeletes-quoted-space", "receive.denyDeletes", ' " true"'),     # git: bad boolean
    ("denydeletes-upper", "receive.denyDeletes", " TRUE"),               # git honours it; the play wrote `true`
]


@pytest.mark.parametrize("key, raw", [(k, r) for _n, k, r in _VALUE_SPELLINGS],
                         ids=[n for n, _k, _r in _VALUE_SPELLINGS])
def test_a_play_key_whose_value_is_not_the_one_the_play_wrote_is_named(tmp_path, capsys, key, raw):
    """A key the play sets, given — once — a spelling of its value that is not the bytes the play writes: an
    upper-case or space-padded `core.sharedRepository`, a quoted-space or upper-case `receive.denyDeletes`. The
    audit says `config-value` for it, and `--strict` exits 3.

    Guards the compare against being read as a normaliser. git does not take `core.sharedRepository = GROUP`
    (receive-pack refuses the push — RAN-L(op)) and treats `receive.denyDeletes = " true"` as a bad boolean; a
    value compared after `.strip().lower()` matched the play's and the canonical was reported CLEAN while a push
    into it broke. The audit's contract is `is not set to what the play sets it to`, and the play sets the
    canonical form — so the value is compared byte for byte."""
    r = _as_the_play_makes_it(tmp_path)
    sec, name = key.split(".")
    R.git(r.bare, "config", "--unset-all", key)         # the play's own line out, exactly one of ours in
    with open(os.path.join(r.bare, "config"), "a", encoding="utf-8") as fh:
        fh.write("[%s]\n\t%s =%s\n" % (sec, name, raw))
    assert _report(r.bare, "--strict", capsys=capsys) == (3, ["config-value: " + key.lower()])


_PACKED_GIT_REFUSES = [
    ("a-second-pack-refs-header-line",
     "# pack-refs with: peeled \n%s refs/heads/main\n# pack-refs with: x\n"),
    ("a-first-line-with-the-header-as-a-substring",
     "zzz# pack-refs with: \n%s refs/heads/main\n"),
]


@pytest.mark.parametrize("body", [b for _n, b in _PACKED_GIT_REFUSES], ids=[n for n, _b in _PACKED_GIT_REFUSES])
def test_a_packed_refs_line_only_the_first_pack_refs_header_is_passed_over(tmp_path, capsys, body):
    """A `packed-refs` whose SECOND line is `# pack-refs with: …`, and one whose FIRST line only CONTAINS that
    text rather than beginning with it: git refuses the whole file for each (RAN-L, `for-each-ref` rc 128), and
    the audit says `unread`.

    Guards the one line of `packed-refs` the audit passes over — git's own first-line header — against being
    matched anywhere, or as a substring. Either would make the audit pass over a line git refuses, and report a
    file git will not read as clean."""
    r = _as_the_play_makes_it(tmp_path)
    _put(r.bare, "packed-refs", body % _ID)
    assert _report(r.bare, capsys=capsys) == (0, ["unread: packed-refs (a line that is not a ref)"])


def test_a_caret_line_under_one_of_the_tools_own_refs_is_named_even_after_a_tag(tmp_path, capsys):
    """`packed-refs` holding a tag, then `refs/heads/main`, then a `^<id>` line: the tag is a `packed-ref`, and
    the `^` line — which sits under `main`, not under the tag — is `unread`.

    Guards the peeled-line machine against carrying `peelable` across the ref between. A `^` line under one of
    the tool's own refs is what is left when a tag's line was removed by hand (git still reads the file); the
    audit names it so the leftover is seen. Without the per-line reset, a tag earlier in the file let the `^`
    line pass as the tool's own."""
    r = _as_the_play_makes_it(tmp_path)
    _put(r.bare, "packed-refs", "# pack-refs with: peeled fully-peeled sorted \n"
         "%s refs/tags/v\n%s refs/heads/main\n^%s\n" % ("1" * 39 + "2", _ID, _ID))
    assert _report(r.bare, capsys=capsys) == (0, ["packed-ref: refs/tags/v",
                                                  "unread: packed-refs (a line that is not a ref)"])


def test_a_play_key_present_twice_with_its_own_value_is_still_named(tmp_path, capsys):
    """A key the play sets, added a SECOND time with the play's OWN value (so the value is right but the line is
    not the play's): the audit says `config-value`.

    Guards the `there more than once` half of the compare against being read as `there more than once with a
    different value`. A key in `config` twice is one the play's plain `git config` would refuse to set (it is
    why the play uses `--replace-all`); a second line nobody put there is a finding whatever its value."""
    r = _as_the_play_makes_it(tmp_path)
    R.git(r.bare, "config", "--add", "receive.denyDeletes", "true")
    assert _report(r.bare, "--strict", capsys=capsys) == (3, ["config-value: receive.denydeletes"])


def test_a_packed_ref_whose_object_name_is_upper_case_hex_is_named_a_ref(tmp_path, capsys):
    """A tag listed in `packed-refs` whose object name is UPPER-case hex: git reads it (it takes an object
    name in either case), so the audit names it `packed-ref`, not `unread`.

    Guards the oid patterns against matching lower-case hex alone. git lists such a ref (`for-each-ref`
    rc 0, measured); a reader that calls the line `unread: packed-refs (a line that is not a ref)` fails
    to name a stray ref by its own kind — and would over-report `unread` for one of the tool's own refs
    written that way."""
    r = _as_the_play_makes_it(tmp_path)
    header = "# pack-refs with: peeled fully-peeled sorted \n"
    upper = ("abcdef0123456789" * 3)[:40].upper()     # a 40-hex object name in upper case (letters differ)
    _put(r.bare, "packed-refs", header + "%s refs/tags/up\n" % upper)
    assert _report(r.bare, "--strict", capsys=capsys) == (3, ["packed-ref: refs/tags/up"])
