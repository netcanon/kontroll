#!/usr/bin/env python3
"""Keep `STATUS.md` honest: a dated page, compared with the tree it describes.

`STATUS.md` (the repository root) says what is released, what works, what is known broken, what is next and which
decisions are open. Every line of it is written by hand; nothing is generated into it. This check compares the
three dated lines with the tree:

  **As of:** YYYY-MM-DD          must parse, must not be later than the reference date; older than 30 days → WARN
  **Images:** vX.Y.Z — …         must equal the `tag:` of every image in docker/images.lock.yml (the release whose
                                 published images this tree deploys — the lock is re-pinned after a tag, so at
                                 the tagged commit itself it still names the release before) → FAIL otherwise
  **Last proven live:** YYYY-MM-DD — …   must parse, must not be later than the as-of date; older than 30 days → WARN

and FAILS a page with a required heading missing, or longer than one page (MAX_LINES): "at most one page" is a
rule only if something refuses page two. On an instance tree, an `instance/OPERATIONS.md` whose as-of line is still
the template's placeholder, or older than 30 days, is a WARN — never a failure, so a freshly scaffolded node does
not go red.

The REFERENCE DATE is the committer date of HEAD (`git log -1 --format=%cs`), so an age is a function of the commit
and the same on every machine; where the tree is not a git checkout (a bundle) it is today. A date is "in the
future" only when it is later than both that date and today, so a page dated today can be checked before it is
committed.

Exit 0: ok. Exit 3: a warning (tests/validate.sh shows it as WARN and does not fail the run, also under --strict:
`main` must not turn red by the passage of time alone). Exit 1: a failure. Exit 2: a usage error. With
`--release` a warning exits 0: a workflow step has no third outcome, and at a release only a FAILURE refuses the
publish — the warnings (a last live run, an unfilled ledger) are the `::warning` annotations.

`--release` (the publish workflow's gate): a stale as-of date is a FAILURE — a stranger pulling the new images
reads the page that shipped with them. Under GitHub Actions a warning is also printed as a `::warning` annotation,
which shows on the run and the pull request without failing the job.
"""
import argparse
import datetime
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATUS = "STATUS.md"
LOCK = os.path.join("docker", "images.lock.yml")
LEDGER = os.path.join("instance", "OPERATIONS.md")

HEADINGS = ("## What works", "## Known broken, or never run live", "## Next", "## Waiting on a decision",
            "## Leading indicators", "## Where the rest is")
MAX_LINES = 80
MAX_AGE_DAYS = 30
OK, FAIL, WARN = 0, 1, 3

_AS_OF = re.compile(r"^\*\*As of:\*\*[ \t]*(\S*)", re.M)
_IMAGES = re.compile(r"^\*\*Images:\*\*[ \t]*(\S*)", re.M)
_LIVE = re.compile(r"^\*\*Last proven live:\*\*[ \t]*(\S*)", re.M)
_TAG = re.compile(r"^[ \t]+tag:[ \t]*[\"']?([^\"'\s]+)[\"']?[ \t]*$", re.M)
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")


def _date(value):
    """The date `value` names in the form YYYY-MM-DD, or None."""
    if not _ISO.fullmatch(value or ""):
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def _line_of(text, pattern):
    """The 1-based line of `pattern`'s first match in `text`, or 1."""
    m = pattern.search(text)
    return text.count("\n", 0, m.start()) + 1 if m else 1


def check(text, lock_text, ref_date, release=False, latest=None):
    """`(level, messages)` for the page `text` against the image lock `lock_text`, on `ref_date`. `level` is OK,
    WARN or FAIL; each message is `(line, sentence)`. A date is in the future when it is later than `latest`
    (default `ref_date`; the command passes the later of HEAD's date and today, so a page dated today can be
    checked before it is committed); ages are counted from `ref_date`."""
    latest = max(ref_date, latest or ref_date)
    fails, warns = [], []
    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        fails.append((MAX_LINES + 1, "the page is %d lines; it may be at most %d (one page)" % (len(lines), MAX_LINES)))
    for heading in HEADINGS:
        if heading not in lines:
            fails.append((1, "the heading %r is missing" % heading))

    m = _AS_OF.search(text)
    as_of = _date(m.group(1)) if m else None
    if m is None:
        fails.append((1, "there is no `**As of:** YYYY-MM-DD` line"))
    elif as_of is None:
        fails.append((_line_of(text, _AS_OF), "the as-of date is not a date in the form YYYY-MM-DD"))
    elif as_of > latest:
        fails.append((_line_of(text, _AS_OF), "the as-of date %s is later than %s" % (as_of, latest)))
    elif (ref_date - as_of).days > MAX_AGE_DAYS:
        stale = (_line_of(text, _AS_OF), "the page is %d days old (as of %s; more than %d days)"
                 % ((ref_date - as_of).days, as_of, MAX_AGE_DAYS))
        (fails if release else warns).append(stale)

    m = _IMAGES.search(text)
    tags = sorted(set(_TAG.findall(lock_text or "")))
    if m is None or not m.group(1):
        fails.append((1, "there is no `**Images:** vX.Y.Z` line"))
    elif not tags:
        fails.append((_line_of(text, _IMAGES), "docker/images.lock.yml names no image tag to compare with"))
    elif tags != [m.group(1)]:
        fails.append((_line_of(text, _IMAGES), "the page says images %s; docker/images.lock.yml pins %s"
                      % (m.group(1), ", ".join(tags))))

    m = _LIVE.search(text)
    live = _date(m.group(1)) if m else None
    if m is None:
        fails.append((1, "there is no `**Last proven live:** YYYY-MM-DD` line"))
    elif live is None:
        fails.append((_line_of(text, _LIVE), "the last-proven-live date is not a date in the form YYYY-MM-DD"))
    elif as_of is not None and live > as_of:
        fails.append((_line_of(text, _LIVE), "the last-proven-live date %s is later than the as-of date" % live))
    elif (ref_date - live).days > MAX_AGE_DAYS:
        warns.append((_line_of(text, _LIVE), "nothing has been proven live for %d days (since %s)"
                      % ((ref_date - live).days, live)))

    if fails:
        return FAIL, fails + warns
    return (WARN if warns else OK), warns


def check_ledger(text, ref_date):
    """`(level, messages)` for an instance's `instance/OPERATIONS.md`: never FAIL. `text` None (no ledger) is OK."""
    if text is None:
        return OK, []
    m = _AS_OF.search(text)
    as_of = _date(m.group(1)) if m else None
    if as_of is None:
        return WARN, [(_line_of(text, _AS_OF), "instance/OPERATIONS.md has no as-of date yet (still the template's)")]
    if as_of > ref_date or (ref_date - as_of).days > MAX_AGE_DAYS:
        return WARN, [(_line_of(text, _AS_OF), "instance/OPERATIONS.md is dated %s" % as_of)]
    return OK, []


def reference_date(root):
    """The committer date of HEAD in `root`, or today where that is not a git checkout."""
    try:
        out = subprocess.run(["git", "-C", root, "log", "-1", "--format=%cs"], capture_output=True, text=True,
                             timeout=30)
        got = _date(out.stdout.strip()) if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        got = None
    return got or datetime.date.today()


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Compare STATUS.md's dated lines with the tree.")
    ap.add_argument("--release", action="store_true", help="a stale as-of date is a failure (the publish gate)")
    ap.add_argument("--root", default=ROOT, help="the tree to check (default: this checkout)")
    args = ap.parse_args(argv)
    ref = reference_date(args.root)
    text = _read(os.path.join(args.root, STATUS))
    if text is None:
        print("check-status: FAIL — there is no %s" % STATUS)
        return FAIL
    level, messages = check(text, _read(os.path.join(args.root, LOCK)) or "", ref, release=args.release,
                            latest=datetime.date.today())
    lvl2, msgs2 = check_ledger(_read(os.path.join(args.root, LEDGER)), ref)
    level = FAIL if FAIL in (level, lvl2) else (WARN if WARN in (level, lvl2) else OK)
    actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for name, msgs in ((STATUS, messages), (LEDGER.replace(os.sep, "/"), msgs2)):
        for line, sentence in msgs:
            print("check-status: %s:%d: %s" % (name, line, sentence))
            if actions and level != FAIL:
                print("::warning file=%s,line=%d::%s" % (name, line, sentence))
    word = {OK: "ok", WARN: "WARN", FAIL: "FAIL"}[level]
    print("check-status: %s (reference date %s%s)" % (word, ref, ", release" if args.release else ""))
    if args.release and level == WARN:
        # A workflow step has two outcomes, not three: at a release a warning is the annotation above and the
        # publish goes on; only a FAILURE (a stale page, another release than the lock's) refuses it.
        return OK
    return level


if __name__ == "__main__":
    sys.exit(main())
