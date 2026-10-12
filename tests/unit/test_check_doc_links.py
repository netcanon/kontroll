"""tests/check-doc-links.py — every relative link in a tracked Markdown file resolves inside the repository.

WHY: the root SECURITY.md wrote each relative link as if it sat one directory down (`](../docs/…)`). From the
repository root `../` leaves the repository, so on the published page every reference of the control document was
a 404, and no gate read links. The checker is a validate step now; these tests hold the things that decide
whether it stays switched on and stays worth having: it reports the defect it was written for, it does not fail a
build over a target that cannot exist in the shipped tree by design, it does not mistake an example in a code
block for a link — and it does not go quietly blind (a fence it mis-reads swallows the rest of a file).
"""
import importlib.util
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHECKER = os.path.join(ROOT, "tests", "check-doc-links.py")

_spec = importlib.util.spec_from_file_location("check_doc_links", CHECKER)
links = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(links)

BT3, BT4 = "`" * 3, "`" * 4


def _tree(tmp_path, files):
    """A scratch tree (not a git repository: the checker then walks the directory) holding `files`."""
    root = tmp_path / "repo"
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_a_link_that_climbs_out_of_the_tree_is_flagged(tmp_path):
    """A root-level file that links `../docs/x.md` is reported with its line — also when a file of that name
    happens to exist one directory above the repository — and so are a target that does not exist and an
    absolute `/path` that does not. Guards the exact SECURITY.md defect: every link written as if the file sat
    one level down. "It exists on my machine" must not make it pass; on the published page it is outside the
    repository."""
    root = _tree(tmp_path, {
        "SECURITY.md": "intro\n\nsee [the runbook](../docs/runbook.md) and [gone](docs/gone.md)\n[abs](/docs/nope.md)\n",
        "docs/runbook.md": "ok\n",
    })
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "runbook.md").write_text("a file OUTSIDE the repository, at the path the link climbs to\n",
                                                  encoding="utf-8")
    assert links.broken_links(str(root)) == [("SECURITY.md", 3, "../docs/runbook.md"),
                                             ("SECURITY.md", 3, "docs/gone.md"),
                                             ("SECURITY.md", 4, "/docs/nope.md")]


def test_links_that_resolve_are_not_reported(tmp_path):
    """Relative links to a file and to a directory, a link from a nested file up to the root, an absolute
    `/path`, a link with a `#fragment`, with a title, in angle brackets, an image, a reference definition and a
    target with a percent-encoded space all pass when their target exists; a footnote definition is not a link.
    Guards the checker being wrong about a link that works — the fastest way for a gate to be switched off."""
    root = _tree(tmp_path, {
        "README.md": "[a](docs/guide.md) [dir](docs/) [frag](docs/guide.md#part-two) [t](docs/guide.md \"title\")\n"
                     "[angle](<docs/guide.md>) ![img](docs/pic.png) [abs](/docs/guide.md) [sp](docs/two%20words.md)\n\n"
                     "[ref]: docs/guide.md\n\nA claim.[^1]\n\n[^1]: a note that is not a path\n",
        "docs/guide.md": "[up](../README.md) [sibling](pic.png) [self](#top)\n",
        "docs/pic.png": "not really an image\n",
        "docs/two words.md": "ok\n",
    })
    assert links.broken_links(str(root)) == []


def test_a_dossier_or_overlay_target_is_exempt_and_records_are_not_read(tmp_path):
    """A link into a dated run folder of `docs/reviews/` that this tree does NOT carry, and a link into
    `instance/`, are not reported although neither target exists; a wrong file name inside a run folder the tree
    DOES carry is reported, and so is a typo next to the folders; and CHANGELOG.md, the files inside a dated run
    folder, and the private overlay's own documents (`instance/…`, which an instance repository tracks) are not
    read at all. Guards the check failing every public build over targets that are unresolvable BY DESIGN — the
    design records written before the release live only in the private repository, and the overlay exists only on
    a deployed node — and failing an instance repository's build over a document that is the operator's; while a
    typo still counts, also behind the exemption's own pattern."""
    root = _tree(tmp_path, {
        "docs/design.md": "[record](reviews/2026-06-13-roadmap/01-design.md) [key](../instance/.sops.yaml)\n"
                          "[index](reviews/INDEX.md) [typo](reviews/READMEE.md)\n"
                          "[here](reviews/2026-10-12-run/10-report.md) [wrong name](reviews/2026-10-12-run/nowhere.md)\n",
        "docs/reviews/README.md": "what lives here\n",
        "docs/reviews/INDEX.md": "| run |\n",
        "docs/reviews/2026-10-12-run/10-report.md": "[broken](nowhere.md)\n",
        "CHANGELOG.md": "[old](docs/reviews/gone/x.md) [older](local/notes.md)\n",
        "instance/secrets/README.md": "[the operator's own note](../../nowhere.md)\n",
        "instance.example/secrets/README.md": "[shipped, and checked](../../docs/design.md)\n",
    })
    assert links.broken_links(str(root)) == [("docs/design.md", 2, "reviews/READMEE.md"),
                                             ("docs/design.md", 3, "reviews/2026-10-12-run/nowhere.md")]
    assert links.markdown_files(str(root)) == ["docs/design.md", "docs/reviews/INDEX.md", "docs/reviews/README.md",
                                               "instance.example/secrets/README.md"]


def test_links_inside_code_and_external_urls_are_ignored(tmp_path):
    """A link shape inside a fenced block (backticks or tildes), inside an inline code span, an external URL, a
    `mailto:`, a protocol-relative `//host`, and a bare `#anchor` are not reported; a real broken link on the
    line after a closed fence is. Guards the false positives that get a link check removed: documentation ABOUT
    links — this repository's own review notes quote `](../docs/…)` — and anything that would need the network."""
    root = _tree(tmp_path, {
        "notes.md": "The defect was `](../docs/x.md)` in SECURITY.md.\n\n```md\n[in a fence](../docs/x.md)\n```\n\n"
                    "~~~\n[in a tilde fence](missing.md)\n~~~\n[after](missing-after.md)\n"
                    "[site](https://example.test/a) [mail](mailto:a@example.test) [pr](//example.test/x) [top](#top)\n",
    })
    assert links.broken_links(str(root)) == [("notes.md", 10, "missing-after.md")]


@pytest.mark.parametrize("why,text,expected", [
    ("a shorter fence line inside a longer fence does not close it",
     "%s\n[shown](inside-1.md)\n%s\n[shown](inside-2.md)\n%s\n[real](after.md)\n" % (BT4, BT3, BT4),
     [(6, "after.md")]),
    ("a line with an info string does not close a fence of its own length",
     "%s\n[shown](inside-1.md)\n%syaml\n[shown](inside-2.md)\n%s\n[real](after.md)\n" % (BT3, BT3, BT3),
     [(6, "after.md")]),
    ("a code span that starts its line is not a fence",
     "%s inline %s\n[real](after.md)\n" % (BT3, BT3), [(2, "after.md")]),
    ("a tilde fence is not closed by backticks", "~~~\n%s\n[shown](inside.md)\n~~~\n[real](after.md)\n" % BT3,
     [(5, "after.md")]),
    ("a fence left open runs to the end of the file", "%s\n[shown](inside.md)\n" % BT3, []),
    ("a footnote definition is not a reference definition", "[^1]: note text\n[name]: real.md\n", [(2, "real.md")]),
], ids=lambda v: v.replace(" ", "-") if isinstance(v, str) and len(v) < 70 and "\n" not in v else None)
def test_a_fence_is_read_as_a_markdown_reader_reads_it(why, text, expected):
    """`link_targets` on text whose fences are the awkward ones: a three-backtick line inside a four-backtick
    fence, a line carrying an info string inside a fence of its own length (a closer carries none), a code span
    that begins a line, backticks inside a tilde fence, a fence never closed — and a footnote definition, which has the shape of a reference definition. Guards the two ways
    this gate goes quietly BLIND rather than red: a fence closed too early re-opens at the real closer and
    swallows every link after it, and a one-line span taken for an opener swallows the rest of the file."""
    assert links.link_targets(text) == expected, why


def _git(tree, *args):
    return subprocess.run(["git", "-C", str(tree)] + list(args), check=True, capture_output=True, text=True).stdout


@pytest.mark.usefixtures("isolated_git")
def test_in_a_git_tree_only_what_is_tracked_counts_and_names_are_exact(tmp_path):
    """In a git tree the checker reads the TRACKED Markdown and resolves against the tracked names, spelled
    exactly: a link whose case differs from the tracked file's is reported, a link to a file that is on disk and
    not tracked is reported, a link to a directory that holds a tracked file passes, and a broken link inside an
    UNTRACKED document is not reported. Guards "green here, red in CI" — a case-insensitive disk resolves
    `guide.md` to `Guide.md`, and an ignored note satisfies a link that is dead on the published page — and the
    word "tracked", which three documents promise."""
    root = _tree(tmp_path, {
        "README.md": "[right](docs/Guide.md) [wrong case](docs/guide.md) [a note](notes.md) [dir](docs)\n",
        "docs/Guide.md": "ok\n",
        "notes.md": "on disk, never added\n",
        "draft.md": "[an untracked document's own broken link](nowhere.md)\n",
    })
    _git(root, "init", "-q")
    _git(root, "add", "README.md", "docs/Guide.md")
    assert links.markdown_files(str(root)) == ["README.md", "docs/Guide.md"]
    assert links.broken_links(str(root)) == [("README.md", 1, "docs/guide.md"), ("README.md", 1, "notes.md")]


def test_a_git_tree_that_git_cannot_list_is_not_walked_instead(tmp_path, monkeypatch):
    """When the root is a git tree and git cannot list it — the program is missing, or it exits non-zero (it
    refuses a tree owned by another user) — `broken_links` raises and the program exits 2 with one line saying so;
    it does not fall back to walking the directory. Outside a git tree the walk is the listing, and it does not
    enter `local/` or an installed `ansible_collections/`. Guards a gate that quietly reads a different set of
    files: a walk of a real checkout opens the operator's untracked notes and third-party READMEs and reports
    their links as this repository's."""
    root = _tree(tmp_path, {"README.md": "[x](missing.md)\n", "local/notes.md": "[y](nowhere.md)\n",
                            "ansible/collections/ansible_collections/ns/name/README.md": "[z](gone.md)\n"})
    assert links.broken_links(str(root)) == [("README.md", 1, "missing.md")]            # no .git: walked, two skipped
    (root / ".git").mkdir()

    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    def refuses(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 128, b"", b"fatal: detected dubious ownership in repository\nhint\n")

    for stand_in, said in ((no_git, "could not be started"), (refuses, "dubious ownership")):
        monkeypatch.setattr(links.subprocess, "run", stand_in)
        with pytest.raises(links.CannotList, match=said):
            links.broken_links(str(root))
    monkeypatch.setattr(sys, "argv", ["check-doc-links.py", str(root)])
    assert links.main() == 2


def test_the_checker_run_as_a_program_names_each_broken_link(tmp_path):
    """Run as validate runs it — a program, given a tree — it exits 1 and prints one `path:line  target` line per
    broken link for a planted violation — also for a target the console cannot encode, which is printed
    escaped — and exits 0 for the same tree once the links are corrected. Guards the step itself: a checker whose
    function is right and whose exit status is always 0 is a green step that checks nothing, and one that dies
    with an encoding error on the line that names the fault reports nothing either."""
    root = _tree(tmp_path, {"SECURITY.md": "[runbook](../docs/runbook.md)\n[caf%s](docs/caf%s.md)\n" % (chr(233), chr(233)),
                            "docs/runbook.md": "ok\n"})
    ascii_console = dict(os.environ, PYTHONIOENCODING="ascii")       # a console that cannot encode every target
    planted = subprocess.run([sys.executable, "-B", CHECKER, str(root)], capture_output=True, text=True,
                             env=ascii_console)
    assert planted.returncode == 1, planted.stderr
    assert "  SECURITY.md:1  ../docs/runbook.md" in planted.stdout.splitlines()
    assert "  SECURITY.md:2  docs/caf\\xe9.md" in planted.stdout.splitlines()
    (root / "SECURITY.md").write_text("[runbook](docs/runbook.md)\n", encoding="utf-8")
    fixed = subprocess.run([sys.executable, "-B", CHECKER, str(root)], capture_output=True, text=True,
                           env=ascii_console)
    assert fixed.returncode == 0 and "SECURITY.md" not in fixed.stdout


def test_the_shipped_documents_have_no_broken_link():
    """Every relative link in this repository's tracked Markdown resolves. The pytest seat of the `doc-links`
    validate step, so the plain test job reports a broken link too. Guards the next document that is moved, or
    written from a path one level off."""
    assert links.broken_links(ROOT) == []
