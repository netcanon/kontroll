"""The play's ownership pass (`kontroll-canonical.py --normalise`) with REAL uids: the chown half (C10, F-CANON).

WHY: `local-canonical.yml` makes every entry of an existing canonical outside `hooks/` `<operator>:1001` — the
owner half is what lets the operator's first push succeed before its group membership is effective — and the
rows of tests/unit/test_canonical_normalise.py cannot show a change of OWNER: they run as whoever runs the
suite, who may give a file to nobody else. These rows run as root and give a root-made canonical to another uid,
and hold that the pass gives it to every regular file and directory outside `hooks/` and to nothing else: not to
a link's own entry, not to what a link points at outside the tree, not to `hooks/`.

It needs ROOT and a Linux kernel. tests/validate.sh runs this file in its `canon-drop-as-root` step (directly as
root, or through `sudo -n`), and that step fails when a row here was SKIPPED; in a plain `pytest` run by an
ordinary user every row is skipped.
"""
import importlib.util
import os
import shutil
import stat
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tests", "unit"))      # the canonical builder lives beside the unit rows

import _canon_repo as R  # noqa: E402
from kontroll import canon  # noqa: E402

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("isolated_git"),
    pytest.mark.skipif(not (hasattr(os, "geteuid") and os.geteuid() == 0 and sys.platform.startswith("linux")),
                       reason="needs root on Linux (it gives a canonical to another uid) — tests/validate.sh runs it as root"),
]

OPERATOR = 1000                      # a uid that is neither root nor the service: the play's `operator_uid`


def _tool():
    spec = importlib.util.spec_from_file_location("kontroll_canonical_pass_root",
                                                  os.path.join(ROOT, "scripts", "kontroll-canonical.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def world():
    """A directory of root's under /tmp, removed after the row."""
    path = tempfile.mkdtemp(prefix="kontroll-pass-", dir="/tmp")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _build(world):
    class _Under:
        def __truediv__(self, leaf):
            return os.path.join(world, leaf)
    r = R.build(_Under())
    with open(os.path.join(r.bare, "hooks", "update"), "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(os.path.join(r.bare, "hooks", "update"), 0o755)
    return r


def _owners(bare):
    """Every entry of the tree by path from the top: `(uid, gid, mode)` of the entry itself (a link: the link)."""
    out = {}
    for here, dirs, files in os.walk(bare):
        for name in dirs + files:
            path = os.path.join(here, name)
            st = os.lstat(path)
            out[os.path.relpath(path, bare).replace(os.sep, "/")] = (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode))
    return out


def _run(tool, bare, *flags, capsys):
    rc = tool.main(["--normalise", bare, "--owner", str(OPERATOR), "--group", str(canon.SERVICE_GID)] + list(flags))
    said = capsys.readouterr()
    return rc, said.out.splitlines(), said.err


def test_every_entry_outside_hooks_is_given_to_the_operator_and_the_service_group_and_nothing_else_is(world, capsys):
    """A canonical root made (every entry root:root, as `git init` leaves it), with a symbolic link inside it to a
    root-owned file outside (0600) and one to a root-owned directory outside (0700, holding a file): `--check`
    exits 3, names an `owner` line for the top directory and for entries outside `hooks/`, names both links, and
    changes no owner; the apply exits 0, and then every regular file and directory outside `hooks/` is
    `1000:1001` with the group's write bit (a directory: and setgid), `hooks/` and everything in it is still
    root's, the links' own entries are still root's, the outside file and directory and what is in it are
    still root's with the modes they had; the next `--check` exits 0 with only the two `link` lines, and the
    next apply changes nothing.

    Guards the F-CANON owner half with the uids the play uses, and the pass's one promise where it costs
    something: a `chown -h` by path gave the link's own entry to the operator and a `chown` by path gave the
    file behind it — here neither moves, because nothing is changed but through the descriptor of what was
    opened inside the tree."""
    tool = _tool()
    r = _build(world)
    outside = os.path.join(world, "outside")
    os.makedirs(os.path.join(outside, "dir"))
    for rel in ("file", "dir/inner"):
        with open(os.path.join(outside, rel), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        os.chmod(os.path.join(outside, rel), 0o600)
    os.chmod(os.path.join(outside, "dir"), 0o700)
    os.symlink(os.path.join(outside, "file"), os.path.join(r.bare, "refs", "to-a-file"))
    os.symlink(os.path.join(outside, "dir"), os.path.join(r.bare, "objects", "to-a-directory"))
    before = _owners(r.bare)
    assert all(uid == 0 and gid == 0 for uid, gid, _m in before.values()), "control: root made all of it"

    rc, lines, err = _run(tool, r.bare, "--check", capsys=capsys)
    assert rc == 3 and err == "", (rc, err)
    assert "owner: HEAD" in lines and "mode: HEAD" in lines
    outside_hooks = [rel for rel in before if rel != "hooks" and not rel.startswith("hooks/")]
    for kind in ("owner", "mode"):                        # the top, every file and directory, not the two links
        assert "unread: %d more `%s` lines are not shown" % (len(outside_hooks) - 2 + 1 - tool._MAX_PER_KIND, kind) \
            in lines, lines
    assert {"link: objects/to-a-directory", "link: refs/to-a-file"} <= set(lines)
    assert not [line for line in lines if "hooks" in line], lines
    assert _owners(r.bare) == before, "a check changed something"

    rc, lines, err = _run(tool, r.bare, capsys=capsys)
    assert rc == 0 and err == "", (rc, err, lines)
    after = _owners(r.bare)
    assert set(after) == set(before)
    top = os.stat(r.bare)
    assert (top.st_uid, top.st_gid, stat.S_IMODE(top.st_mode)) == (OPERATOR, canon.SERVICE_GID, 0o2775)
    for rel, (uid, gid, mode) in after.items():
        was = before[rel][2]
        if rel == "hooks" or rel.startswith("hooks/") or rel.endswith("/to-a-file") or rel.endswith("/to-a-directory"):
            assert (uid, gid, mode) == (0, 0, was), rel
        elif os.path.isdir(os.path.join(r.bare, rel)):
            assert (uid, gid, mode) == (OPERATOR, canon.SERVICE_GID, was | 0o2020), rel
        else:
            assert (uid, gid, mode) == (OPERATOR, canon.SERVICE_GID, was | 0o020), rel
    for rel, want in (("file", 0o600), ("dir", 0o700), ("dir/inner", 0o600)):
        st = os.lstat(os.path.join(outside, rel))
        assert (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) == (0, 0, want), rel

    assert _run(tool, r.bare, "--check", capsys=capsys)[:2] == (0, ["link: objects/to-a-directory",
                                                                   "link: refs/to-a-file"])
    assert _run(tool, r.bare, capsys=capsys)[:2] == (0, ["link: objects/to-a-directory", "link: refs/to-a-file"])
    assert _owners(r.bare) == after

    os.chown(r.bare, 0, 0)                                # the top directory itself, given back to root
    assert _run(tool, r.bare, "--check", capsys=capsys)[:2] == (3, ["link: objects/to-a-directory",
                                                                   "link: refs/to-a-file", "owner: the top directory"])
    assert _run(tool, r.bare, capsys=capsys)[:2] == (0, ["link: objects/to-a-directory", "link: refs/to-a-file",
                                                        "owner: the top directory"])
    top = os.stat(r.bare)
    assert (top.st_uid, top.st_gid, stat.S_IMODE(top.st_mode)) == (OPERATOR, canon.SERVICE_GID, 0o2775)


def test_what_the_service_wrote_is_given_to_the_operator_and_a_setgid_file_loses_nothing_it_needs(world, capsys):
    """A proposal's ref file and a loose object written as the service (uid and gid 1001) into a canonical that is
    otherwise the operator's, and a group-executable regular file with the setgid bit, which Linux clears on a
    change of owner (`chown(2)`: since 2.2.13, for root too): the apply exits 0, names exactly those entries,
    and gives them to `1000:1001` with the group's write bit; the file that had setgid is `0674` afterwards —
    the cleared bit is NOT written back.

    Guards the case the grant exists for — the next deploy after an armed one, when the service's proposals
    stand beside the operator's objects — and the order inside the pass: the owner is set first and the mode
    read again, so `fchmod` adds to the mode the file then has instead of writing back a stale one (which
    would restore a setgid bit the kernel had just taken off)."""
    tool = _tool()
    r = _build(world)
    assert _run(tool, r.bare, capsys=capsys)[0] == 0
    theirs = [os.path.join(r.bare, "refs", "heads", "proposed", R.FF),
              os.path.join(r.bare, "objects", "info", "a-loose-thing")]
    with open(theirs[1], "w", encoding="utf-8") as fh:
        fh.write("x\n")
    for path in theirs:
        os.chown(path, canon.SERVICE_UID, canon.SERVICE_GID)
        os.chmod(path, 0o644)
    os.chown(os.path.join(r.bare, "description"), 0, canon.SERVICE_GID)
    os.chmod(os.path.join(r.bare, "description"), 0o2654)
    assert stat.S_IMODE(os.stat(os.path.join(r.bare, "description")).st_mode) == 0o2654, "control: setgid is on"
    rc, lines, _err = _run(tool, r.bare, capsys=capsys)
    assert rc == 0
    assert sorted(lines) == ["mode: description", "mode: objects/info/a-loose-thing",
                             "mode: refs/heads/proposed/" + R.FF, "owner: description",
                             "owner: objects/info/a-loose-thing", "owner: refs/heads/proposed/" + R.FF]
    for path in theirs:
        st = os.stat(path)
        assert (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) == (OPERATOR, canon.SERVICE_GID, 0o664)
    st = os.stat(os.path.join(r.bare, "description"))
    assert (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) == (OPERATOR, canon.SERVICE_GID, 0o674)
    assert _run(tool, r.bare, "--check", capsys=capsys)[:2] == (0, [])
