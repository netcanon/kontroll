#!/usr/bin/env python3
"""Write the files of ONE commit of a repository into a directory — the commit as it is (C10).

`git archive <commit> | tar -x` is the commit as its own `.gitattributes`, the repository's filters and its
replacement refs make it: `export-ignore` leaves paths out, `export-subst` rewrites file contents, a filter runs a
program the repository's config names, a tracked symbolic link becomes a link. A step that DECIDES something from a
commit's files — or runs with them — must read the commit and nothing else. This is that read, for a play or a
script: `kontroll.canon.read_tree_verified` (one `git cat-file --batch`, every object re-hashed against its id,
regular files only) and `kontroll.canon.write_files`.

Usage:  python3 scripts/kontroll-export.py --repo <git dir> <commit id> <dest> [<prefix> ...]

  --repo     the repository's GIT directory (a bare repository itself; `<checkout>/.git` for a clone)
  commit id  a FULL commit id — never a ref name: the caller reads the ref once and says which commit it means
  dest       a directory that does not exist yet (it then appears whole, by one rename), or an empty one
  prefix     limit the export to paths under `<prefix>` (written with its trailing slash: `modules/`) or to that
             one file (no slash: `instance/fleet.yml`). A prefix that selects no file of the commit is an error.

Exit 0 and one line naming how many files were written. Exit 2 with one sentence when the commit's tree is
refused (an object that is not what its name says, a link or a submodule entry, a name no checkout could take) —
and for nothing else: a play may act on it. Exit 1 for everything that is not a verdict on the commit: a usage
error, a prefix that selects nothing, a destination that is not this command's to fill, a repository that cannot
be read, files that cannot be moved in. After any exit but 0 the destination is as it was found. The sentence
names the rule, never a path or a value of the commit.

What is written is each file's CONTENT: an executable file of the commit is written like any other (no mode is
carried over). From a play: hand it a path that does not exist, made new for each run (a second run into the
same directory exits 1), and `changed_when: false` — it changes nothing a later run depends on. A process that
is KILLED while it fills a destination that already existed can leave part of the tree there.
"""
import argparse
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # so `from kontroll import canon` resolves
from kontroll import canon


def main(argv):
    ap = argparse.ArgumentParser(description="Write the files of one commit into a directory — the commit as it is.")
    ap.add_argument("--repo", required=True, help="the repository's git directory")
    ap.add_argument("commit", help="a full commit id (not a ref name)")
    ap.add_argument("dest", help="a directory that does not exist yet, or is empty")
    ap.add_argument("prefixes", nargs="*", help="`dir/` or a file path; default: every file")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:                      # argparse exits 2 for a usage error, and 2 is this command's "refused"
        return 0 if e.code in (0, None) else 1
    if not canon.valid_oid(args.commit):
        sys.stderr.write("kontroll-export: the commit must be named by its full id, not by a ref or a short id\n")
        return 1
    dest = os.path.abspath(args.dest)
    if os.path.lexists(dest) and (not os.path.isdir(dest) or os.path.islink(dest) or os.listdir(dest)):
        sys.stderr.write("kontroll-export: the destination exists and is not an empty directory\n")
        return 1
    parent = os.path.dirname(dest)
    if not os.path.isdir(parent):
        sys.stderr.write("kontroll-export: the destination's parent directory does not exist\n")
        return 1
    prefixes = [p.encode("utf-8") for p in args.prefixes] or None
    # Everything is written beside the destination first and moved in only when the whole export succeeded: a
    # refused commit must not leave half a tree where a later step would read it.
    # A destination that is there already (an empty directory the caller made, a mount point) is filled from a
    # stage INSIDE it: a move from beside it would cross onto another file system when it is a mount point.
    there = os.path.isdir(dest)
    try:
        stage = tempfile.mkdtemp(prefix=".kontroll-export-", dir=dest if there else parent)
    except OSError:
        sys.stderr.write("kontroll-export: nothing can be written beside or into the destination\n")
        return 1
    moved = []
    try:
        try:
            files = canon.read_tree_verified(args.repo, args.commit)
            for prefix in prefixes or ():
                if not any(path.startswith(prefix) if prefix.endswith(b"/") else path == prefix for path in files):
                    sys.stderr.write("kontroll-export: the commit holds nothing under one of the prefixes (a directory "
                                     "is written with its trailing slash). Nothing was written.\n")
                    return 1
            written = canon.write_files(files, stage, prefixes)
        except canon.TreeRefused as e:
            sys.stderr.write("kontroll-export: REFUSED — %s. Nothing was written.\n" % e)
            return 2
        except canon.Unreadable as e:
            sys.stderr.write("kontroll-export: the repository could not be read — %s. Nothing was written.\n" % e)
            return 1
        try:
            if there:
                for name in sorted(os.listdir(stage)):
                    os.rename(os.path.join(stage, name), os.path.join(dest, name))
                    moved.append(name)
            else:
                os.chmod(stage, 0o777 & ~_umask())
                os.rename(stage, dest)                   # one rename: the destination appears whole, or not at all
        except OSError:
            for name in moved:                           # take back what was moved: the whole export, or nothing
                path = os.path.join(dest, name)
                shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.remove(path)
            sys.stderr.write("kontroll-export: the files could not be moved into the destination. Nothing was written.\n")
            return 1
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    print("kontroll-export: wrote %d file(s) of %s" % (written, args.commit))
    return 0


def _umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
