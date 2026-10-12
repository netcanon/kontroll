#!/usr/bin/env python3
"""Fail if a relative link in a tracked Markdown file does not resolve inside the repository.

WHY: the root `SECURITY.md` wrote every relative link as if it sat one directory down (`](../docs/…)`); from the
root, `../` leaves the repository, so on the published page each of them was a 404 — the control document's own
references. Nothing checked. This does: `python3 tests/check-doc-links.py` (run by tests/validate.{sh,ps1}).
Exit 0 = every link resolves, 1 = some do not (one `path:line  target` per finding), 2 = it could not look.

What is checked: inline links and images `[text](target)` and reference definitions `[name]: target`, outside
fenced code blocks and inline code spans. A target is resolved against the directory of the file that holds it
(`/x` against the repository root) and must name a TRACKED file, or a directory that holds one, INSIDE the
repository — compared by its exact spelling, so a link that only works on a case-insensitive disk, or to a file
that exists here and is not tracked, is a finding on every machine; and a target that climbs out is a finding
even where something happens to exist there.

What is not: external URLs (nothing is fetched — the check is hermetic), `#fragment` anchors (a heading that is
renamed is not what this is for, and anchor rules differ between renderers), and back-ticked paths in prose. Nor
are these link forms read at all: a reference USE (`[text][name]` — only the definition is resolved), raw HTML
(`href=`, `src=`), a target put on the line after `](`, and a target with a parenthesis in it (it is cut at the
first one). Indented code and HTML comments ARE read: a list continuation indents a real link by four spaces.

Declared exemptions, each because the target cannot exist in the public tree BY DESIGN:
  docs/reviews/<run>/…   the design records. The ones written before the public release live only in the private
                         repository (docs/reviews/README.md says their links "will not resolve here"). Exempt only
                         while the run folder is ABSENT: a link into a folder this tree carries is checked.
  instance/…             the private overlay: it exists on a deployed node, never in the shipped tree.
And the files that are records, not documents: CHANGELOG.md (its header disclaims its own paths) and everything
under a dated folder of docs/reviews/. And the private overlay's own documents (instance/…), where a repository
tracks one: they are the operator's, not this tree's — their shipped twins under instance.example/ are checked.

Which files: inside a git tree, the tracked ones — and if git cannot list them (it is not installed; it refuses
the tree's ownership) the check says so and stops, rather than quietly reading some other set of files. Outside a
git tree (an extracted bundle) it walks the directory.
"""
import os
import re
import subprocess
import sys
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# a target under the private overlay is unresolvable in the public tree on purpose; so is one inside a dated run
# folder of docs/reviews/ that this tree does not carry (_DATED_RUN, below) — docs/reviews/README.md itself is an
# ordinary target
EXEMPT_TARGETS = ("instance/",)
# files whose links are history: not checked
EXEMPT_FILES = ("CHANGELOG.md",)
# ...and the private overlay's own documents (tracked in an instance repository, never shipped)
EXEMPT_DIRS = ("instance/",)
_DATED_RUN = re.compile(r"docs/reviews/\d{4}-\d{2}-\d{2}[^/]*/")
# directories a walk does not enter (only used outside a git tree): nothing under them is this tree's document
_WALK_SKIPS = ("node_modules", "local", "ansible_collections")

_FENCE = re.compile(r"^\s{0,3}(```+|~~~+)")
_CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1")
# [text](target) / ![alt](target), with an optional title and an optional <…> around the target
_INLINE = re.compile(r"\]\(\s*(<[^>\n]*>|[^)\s]+)(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^)\n]*\)))?\s*\)")
# [name]: target — not a footnote definition ([^1]: text)
_REFERENCE = re.compile(r"^\s{0,3}\[(?!\^)[^\]\n]+\]:\s*(<[^>\n]*>|\S+)")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


class CannotList(Exception):
    """This is a git tree and git would not say which files it tracks."""


def tracked_names(root=ROOT):
    """Every tracked path of the git tree at `root`, relative with forward slashes, plus each of their parent
    directories — or None when `root` is not a git tree. Raises CannotList when it is one and git cannot list it:
    falling back to a walk there would read the operator's untracked notes and call the result this check."""
    if not os.path.exists(os.path.join(root, ".git")):
        return None
    try:
        listing = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True)
    except OSError as exc:
        raise CannotList("git could not be started (%s)" % exc.__class__.__name__)
    if listing.returncode != 0:
        said = listing.stderr.decode("utf-8", "replace").strip().splitlines()
        raise CannotList(said[0] if said else "git ls-files exited %d" % listing.returncode)
    names = set()
    for path in listing.stdout.decode("utf-8").split("\0"):
        while path:
            names.add(path)
            path = path.rpartition("/")[0]
    return names


def markdown_files(root=ROOT, names=None):
    """The Markdown files to check, repository-relative with forward slashes: the tracked ones, or — outside a git
    tree, e.g. an extracted bundle — every `*.md` found by walking the directory."""
    if names is None:
        names = tracked_names(root)
    if names is not None:
        files = [f for f in names if f.endswith(".md") and os.path.isfile(os.path.join(root, f))]
    else:
        files = []
        for directory, dirs, found in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in _WALK_SKIPS]
            files += [os.path.relpath(os.path.join(directory, n), root).replace(os.sep, "/")
                      for n in found if n.endswith(".md")]
    return sorted(f for f in files
                  if f not in EXEMPT_FILES and not f.startswith(EXEMPT_DIRS) and not _DATED_RUN.match(f))


def link_targets(text):
    """[(line number, target)] for every link target in a Markdown text, skipping fenced code blocks and inline
    code spans. A fence is closed only by a line of the SAME character, at least as long as its opener, with
    nothing after it — so an example fence shown inside a longer one does not end it; and a line that starts with
    backticks and holds another backtick is a code span, not a fence."""
    out, fence = [], None
    for number, line in enumerate(text.split("\n"), 1):
        mark = _FENCE.match(line)
        if mark and fence is None and mark.group(1)[0] == "`" and "`" in line[mark.end():]:
            mark = None                                         # ```code``` on one line: a span that starts the line
        if mark:
            token = mark.group(1)
            if fence is None:
                fence = token
                continue
            if token[0] == fence[0] and len(token) >= len(fence) and not line[mark.end():].strip():
                fence = None
                continue
        if fence is not None:
            continue
        line = _CODE_SPAN.sub(lambda m: " " * len(m.group(0)), line)
        found = [m.group(1) for m in _INLINE.finditer(line)]
        reference = _REFERENCE.match(line)
        if reference:
            found.append(reference.group(1))
        out += [(number, target.strip("<>")) for target in found]
    return out


def broken_links(root=ROOT, files=None):
    """[(file, line number, target)] for every relative link that does not resolve to something inside `root`."""
    root = os.path.abspath(root)
    names = tracked_names(root)

    def exists(rel):
        if names is not None:
            return rel == "." or rel in names
        return os.path.exists(os.path.join(root, rel))

    findings = []
    for rel in (markdown_files(root, names) if files is None else files):
        try:
            text = open(os.path.join(root, rel), encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        base = os.path.dirname(rel)
        for number, target in link_targets(text):
            if not target or target.startswith("#") or _SCHEME.match(target) or target.startswith("//"):
                continue
            path = urllib.parse.unquote(re.split(r"[#?]", target, maxsplit=1)[0])
            if not path:
                continue
            joined = path.lstrip("/") if path.startswith("/") else os.path.join(base, path)
            resolved = os.path.normpath(joined).replace(os.sep, "/")
            if resolved == ".." or resolved.startswith("../"):
                findings.append((rel, number, target))          # climbs out of the repository
                continue
            if (resolved + "/").startswith(EXEMPT_TARGETS):
                continue
            run = _DATED_RUN.match(resolved + "/")
            if run and not exists(run.group(0).rstrip("/")):
                continue                                        # a run folder this tree does not carry
            if not exists(resolved):
                findings.append((rel, number, target))
    return findings


def _say(text):
    """Print, whatever the console can encode: a path or a target may hold a character it cannot, and the report
    must not die on the line that names the fault."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(encoding, "backslashreplace").decode(encoding))


def main(argv=None):
    root = os.path.abspath((argv or sys.argv[1:] or [ROOT])[0])
    try:
        findings = broken_links(root)
    except CannotList as why:
        _say("doc-links: git could not list the files of %s: %s. Nothing was checked." % (root, why))
        return 2
    if findings:
        _say("relative links that do not resolve to a tracked path inside the repository (fix the path: the "
             "spelling is compared exactly; `git add` a new target; a target that cannot exist in the shipped "
             "tree is written as a code span, not as a link):")
        for rel, number, target in findings:
            _say("  %s:%d  %s" % (rel, number, target))
        return 1
    _say("every relative Markdown link resolves")
    return 0


if __name__ == "__main__":
    sys.exit(main())
