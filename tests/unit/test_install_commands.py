"""The install sequence is written ONCE, and every command that is written anywhere else can be run as written.

WHY (the failures these guard). The first-install sequence stood in many places — the quickstart, a README, the
scripts' closing messages, their usage headers — and the copies had drifted apart: a correction to one left the
others as they were, and after the scaffold gained a required `--trust-mode` flag every copy but the quickstart
showed a command that is REFUSED at step one. On the permissive edition the same line, without the flag, is not refused: it
silently arms the box. A stranger follows whichever copy they happen to read first.

So: the quickstart at the top of docs/install-from-scratch.md is the one full sequence; an epilogue prints the ONE
next command and points there; and no scaffold command is written anywhere without an explicit promote posture.
These tests read the tree for both.
"""
import os
import re
import subprocess

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A scaffold command WITH arguments, in either spelling: the launcher verb, or the script it runs. An elided form
# (`fresh-init …`) and a mention in prose (`kontroll-init --fresh` copies the example overlay) are not commands a
# reader can paste, and are not matched.
SCAFFOLD = re.compile(r"\bfresh-init\s+--[A-Za-z]|\bkontroll-init(?:\.py)?\s+--fresh\s+--[A-Za-z]")
# Not instructions: the tests themselves, the changelog (history), and the review dossiers (records).
NOT_INSTRUCTIONS = ("tests/", "CHANGELOG.md", "docs/reviews/")


def _tracked_files():
    """The repository's files, relative with forward slashes: the tracked ones, or every file under the root when
    this is not a git tree (an extracted bundle)."""
    listing = subprocess.run(["git", "-C", ROOT, "ls-files", "-z"], capture_output=True)
    if listing.returncode == 0 and listing.stdout:
        return [f for f in listing.stdout.decode("utf-8").split("\0") if f]
    found = []
    for directory, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        found += [os.path.relpath(os.path.join(directory, n), ROOT).replace(os.sep, "/") for n in names]
    return found


def logical_lines(text):
    """[(line number, text)] with a backslash-continued command joined into one line, numbered where it starts."""
    out, held, start = [], "", 0
    for number, line in enumerate(text.split("\n"), 1):
        if not held:
            start = number
        if line.rstrip().endswith("\\"):
            held += line.rstrip()[:-1] + " "
            continue
        out.append((start, held + line))
        held = ""
    if held:
        out.append((start, held))
    return out


# kontroll-init's three break-glass choices; a scaffold command names one (T11).
BREAK_GLASS_CHOICES = ("--break-glass-recipient", "--mint-break-glass", "--no-break-glass")


def scaffold_commands_without_a_trust_mode(files=None, root=ROOT):
    """["path:line"] of every written scaffold command that does not carry `--trust-mode`."""
    return _scaffold_commands_missing(lambda line: "--trust-mode" in line, files, root)


def scaffold_commands_without_a_break_glass_choice(files=None, root=ROOT):
    """["path:line"] of every written scaffold command that names none of the break-glass choices."""
    return _scaffold_commands_missing(lambda line: any(c in line for c in BREAK_GLASS_CHOICES), files, root)


def _scaffold_commands_missing(carries, files, root):
    """["path:line"] of every written scaffold command, outside the tests, the changelog and the dossiers, whose
    logical line `carries` refuses."""
    missing = []
    for rel in sorted(_tracked_files() if files is None else files):
        if rel.startswith(NOT_INSTRUCTIONS):
            continue
        try:
            text = open(os.path.join(root, rel), encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        missing += ["%s:%d" % (rel, number) for number, line in logical_lines(text)
                    if SCAFFOLD.search(line) and not carries(line)]
    return missing


def test_every_written_scaffold_command_names_a_trust_mode(tmp_path):
    """No document, usage header, comment or epilogue in the tree shows a scaffold command — `fresh-init --…` or
    `kontroll-init --fresh --…` — without `--trust-mode` on the same logical line (a backslash-continued command
    is one line). Guards a copied command that is refused at step one on this edition and, on the permissive
    one, silently arms the box. The scan is shown to find a planted line in both spellings and over a
    continuation, and to leave an elided form and a prose mention alone."""
    assert scaffold_commands_without_a_trust_mode() == [], \
        "add `--trust-mode separated` to the command — or, where the line is a sentence ABOUT the scaffold, reword it " \
        "so that it is not a command a reader could paste"
    planted = {
        "docs/a.md": "run:\n\n    scripts/kontroll-installer.sh fresh-init --mgmt-ip 192.0.2.10 --domain example.test\n",
        "scripts/b.sh": "#   python3 scripts/kontroll-init.py --fresh --mgmt-ip <ip> \\\n#       --domain <domain>\n",
        "docs/c.md": "./kontroll fresh-init --mgmt-ip <ip> \\\n    --domain <domain> --trust-mode separated\n",
        "docs/d.md": "`./kontroll fresh-init …` scaffolds the overlay; `kontroll-init --fresh` copies the example.\n",
        "tests/e.py": "x = 'fresh-init --mgmt-ip 192.0.2.10'\n",
    }
    for rel, text in planted.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert scaffold_commands_without_a_trust_mode(files=list(planted), root=str(tmp_path)) == [
        "docs/a.md:3", "scripts/b.sh:1"]


def test_every_written_scaffold_command_names_a_break_glass_choice(tmp_path):
    """No document, usage header, comment or epilogue in the tree shows a scaffold command without one of
    `--break-glass-recipient`, `--mint-break-glass` or `--no-break-glass` on the same logical line. Guards a copied
    command that `kontroll-init --fresh` refuses at step one since T11 — on every edition — so a stranger following
    any copy but the quickstart stops there. The scan is shown to find a planted line in both spellings and over a
    continuation, and to accept each of the three choices."""
    assert scaffold_commands_without_a_break_glass_choice() == [], \
        "add a break-glass choice to the command (`--break-glass-recipient <age1…>`, docs/break-glass.md)"
    planted = {
        "docs/a.md": "    scripts/kontroll-installer.sh fresh-init --mgmt-ip <ip> --domain <d> --trust-mode separated\n",
        "scripts/b.sh": "#   python3 scripts/kontroll-init.py --fresh --trust-mode separated \\\n#       --mgmt-ip <ip>\n",
        "docs/c.md": "./kontroll fresh-init --trust-mode separated \\\n    --break-glass-recipient <age1…>\n",
        "docs/d.md": "./kontroll fresh-init --trust-mode separated --mint-break-glass\n",
        "docs/e.md": "python3 scripts/kontroll-init.py --fresh --trust-mode solo --no-break-glass\n",
    }
    for rel, text in planted.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert scaffold_commands_without_a_break_glass_choice(files=list(planted), root=str(tmp_path)) == [
        "docs/a.md:1", "scripts/b.sh:1"]


EPILOGUES = ["scripts/install-prereqs.sh", "scripts/install.sh", "scripts/make-launch-kit.sh",
             "scripts/make-bundle-airgap.sh", "scripts/make-bundle.sh"]


def closing_message(text):
    """What a script prints last: the body of its last here-document, or — where it closes with `echo` lines — the
    run of `echo` lines that ends the file."""
    if "<<" in text:
        return text.rsplit("<<", 1)[1]
    tail = []
    for line in reversed(text.rstrip("\n").split("\n")):
        if not line.startswith("echo "):
            break
        tail.append(line)
    return "\n".join(reversed(tail))


@pytest.mark.parametrize("script", EPILOGUES)
def test_a_script_epilogue_hands_over_to_the_one_written_sequence(script):
    """A script that ends by telling the operator what to do next names `docs/install-from-scratch.md` IN THAT
    CLOSING MESSAGE — a mention in a header comment does not count — and does not go on to restate the sequence:
    `configure-semaphore`, the sequence's LAST step, appears nowhere in the script. Guards the "fix one copy, the
    others keep rotting" failure: each of these printed the whole procedure, each copy drifted, and all of them
    showed a scaffold command the scaffold refuses."""
    text = open(os.path.join(ROOT, script), encoding="utf-8").read()
    assert closing_message(text).strip(), "%s: no closing message found — has its last statement changed?" % script
    assert "docs/install-from-scratch.md" in closing_message(text), \
        "%s does not point at the written sequence in what it prints last" % script
    assert "configure-semaphore" not in text, "%s restates the install sequence instead of pointing at it" % script


def test_the_closing_message_is_what_a_script_prints_last():
    """`closing_message` returns the body of the last here-document, or the final run of `echo` lines — and not a
    header comment that happens to name the document. Guards the test above passing on a mention that is not the
    pointer: two of the scripts name the document in a comment or a warning as well."""
    heredoc = "# see docs/install-from-scratch.md\ncat <<'A'\nfirst\nA\ndo_work\ncat <<EOF\nNext: x\nEOF\n"
    assert "Next: x" in closing_message(heredoc) and "install-from-scratch" not in closing_message(heredoc)
    echoes = "# docs/install-from-scratch.md\nwork\necho \"Wrote it\"\necho \"Next: y\"\n"
    assert closing_message(echoes) == "echo \"Wrote it\"\necho \"Next: y\""
