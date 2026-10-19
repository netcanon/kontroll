"""`tests/check-status.py` — STATUS.md is a dated page, compared with the tree it describes.

WHY: where the project stood lived in three places that disagreed, none checked by anything, so each went stale
without a sign. The page is written by hand; this check is what keeps its three dated lines honest. Every row
injects the reference date, so none depends on the clock: the dated comparison stays out of the hermetic suite (a
pytest asserting the age would fail on an unchanged tree), and these rows test the function that makes it.
"""
import datetime
import importlib.util
import os
import subprocess

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
D = datetime.date


def _module():
    spec = importlib.util.spec_from_file_location("check_status", os.path.join(ROOT, "tests", "check-status.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cs = _module()
LOCK = 'images:\n  a:\n    tag: "v1.2.3"\n  b:\n    tag: "v1.2.3"\n'


def _page(as_of="2026-10-01", images="v1.2.3", live="2026-10-01", extra=""):
    head = "# kontroll — status\n\n**As of:** %s\n**Images:** %s — the release\n**Last proven live:** %s — x\n\n" % (
        as_of, images, live)
    return head + "".join("%s\n\n- x\n\n" % h for h in cs.HEADINGS) + extra


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def test_the_committed_status_page_passes_its_own_check():
    """The real STATUS.md and the real image lock pass the check on the page's own as-of date — no heading
    missing, not longer than a page, the images line the lock's tag. (Its last-live date may warn: that is the
    indicator doing its job, not a defect.) Guards a page committed without its date line, with a heading
    dropped, or grown past one page."""
    text = _read("STATUS.md")
    as_of = cs._date(cs._AS_OF.search(text).group(1))
    level, messages = cs.check(text, _read(os.path.join("docker", "images.lock.yml")), as_of)
    assert level in (cs.OK, cs.WARN), messages
    assert all("proven live" in sentence for _line, sentence in messages), messages


def test_the_images_line_must_equal_the_image_lock():
    """An `**Images:**` line naming another release than every `tag:` in docker/images.lock.yml fails; so does a
    lock whose images disagree among themselves; the matching page passes. Guards a release re-pinned without the
    status page being opened — the commit that re-pins the lock is forced to touch the page."""
    ref = D(2026, 10, 1)
    assert cs.check(_page(), LOCK, ref)[0] == cs.OK
    assert cs.check(_page(images="v1.2.2"), LOCK, ref)[0] == cs.FAIL
    assert cs.check(_page(), LOCK.replace('"v1.2.3"\n  b', '"v1.2.2"\n  b'), ref)[0] == cs.FAIL
    assert cs.check(_page(), "images: {}\n", ref)[0] == cs.FAIL


def test_a_stale_page_warns_and_fails_only_at_a_release():
    """A page more than a month older than the reference date is a WARNING — the gate shows it and stays green —
    and a FAILURE only with `release=True`, the publish workflow's mode. A last live run more than a month old
    warns in both modes. Guards the gate turning red by the passage of time on an unchanged tree, and, the other
    way, a release shipping a page nobody has read for a month."""
    ref = D(2026, 12, 1)
    old = _page(as_of="2026-10-01", live="2026-10-01")
    assert cs.check(old, LOCK, ref)[0] == cs.WARN
    assert cs.check(old, LOCK, ref, release=True)[0] == cs.FAIL
    fresh_page_old_run = _page(as_of="2026-11-30", live="2026-10-01")
    assert cs.check(fresh_page_old_run, LOCK, ref)[0] == cs.WARN
    assert cs.check(fresh_page_old_run, LOCK, ref, release=True)[0] == cs.WARN
    assert cs.check(_page(as_of="2026-11-01", live="2026-11-01"), LOCK, ref)[0] == cs.OK      # 30 days: not stale


def test_a_future_date_a_bad_date_a_missing_line_or_a_second_page_fails():
    """An as-of date later than both HEAD's date and today (silencing the warning by dating the page forward), a
    date that does not parse, an absent dated line, a last-live date after the as-of date, a missing heading and a
    page longer than one page each fail. A page dated today passes before it is committed (`latest` = today).
    Guards each way the page can stop saying anything checkable."""
    ref = D(2026, 10, 1)
    assert cs.check(_page(as_of="2026-10-02"), LOCK, ref)[0] == cs.FAIL
    assert cs.check(_page(as_of="2026-10-02", live="2026-10-02"), LOCK, ref, latest=D(2026, 10, 2))[0] == cs.OK
    assert cs.check(_page(as_of="2026-13-01"), LOCK, ref)[0] == cs.FAIL
    assert cs.check(_page(as_of="soon"), LOCK, ref)[0] == cs.FAIL
    assert cs.check(_page().replace("**As of:**", "As of:"), LOCK, ref)[0] == cs.FAIL
    assert cs.check(_page().replace("**Last proven live:**", "Live:"), LOCK, ref)[0] == cs.FAIL
    assert cs.check(_page(live="2026-09-30", as_of="2026-09-29"), LOCK, ref)[0] == cs.FAIL
    assert cs.check(_page().replace("## Next\n", "## Later\n"), LOCK, ref)[0] == cs.FAIL
    assert cs.check(_page(extra="x\n" * cs.MAX_LINES), LOCK, ref)[0] == cs.FAIL


def test_an_unfilled_operations_ledger_warns_and_never_fails():
    """On an instance tree, `instance/OPERATIONS.md` whose as-of line is still the template's placeholder, or more
    than a month old, is a WARNING; a dated one is fine; an absent one (a tool checkout) says nothing. Never a
    failure: a freshly scaffolded node must not fail its first `validate`. The shipped template itself is the
    placeholder case."""
    ref = D(2026, 10, 1)
    template = _read(os.path.join("instance.example", "OPERATIONS.md"))
    assert cs.check_ledger(template, ref)[0] == cs.WARN
    assert cs.check_ledger(template.replace("YYYY-MM-DD", "2026-08-01", 1), ref)[0] == cs.WARN
    assert cs.check_ledger(template.replace("YYYY-MM-DD", "2026-09-20", 1), ref)[0] == cs.OK
    assert cs.check_ledger(None, ref) == (cs.OK, [])


def test_the_command_exits_with_the_level_and_names_the_page(tmp_path, capsys):
    """`main` reads the files under `--root`, prints one line per message naming the file and line, and exits 0, 3
    or 1 — the exit status is what tests/validate.sh's `advise` maps to ok, WARN and FAIL. A tree with no
    STATUS.md fails. Guards the command and the gate disagreeing about what a status is."""
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker" / "images.lock.yml").write_text(LOCK, encoding="utf-8")
    assert cs.main(["--root", str(tmp_path)]) == cs.FAIL
    today = datetime.date.today()
    (tmp_path / "STATUS.md").write_text(_page(as_of=str(today), live=str(today)), encoding="utf-8")
    assert cs.main(["--root", str(tmp_path)]) == cs.OK
    (tmp_path / "STATUS.md").write_text(_page(as_of=str(today), live="2000-01-01"), encoding="utf-8")
    assert cs.main(["--root", str(tmp_path)]) == cs.WARN
    assert "STATUS.md:5: nothing has been proven live" in capsys.readouterr().out


def test_a_release_is_refused_by_a_failure_and_never_by_a_warning(tmp_path):
    """With `--release` the command exits 0 on a warning and 1 on a failure. A workflow step has two outcomes: an
    exit 3 there fails the publish-images `gate` job, so a last live run more than a month old — a warning in both
    modes — would refuse every release until somebody ran the lab. Guards the release refusing on a warning, and a
    stale page passing it."""
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker" / "images.lock.yml").write_text(LOCK, encoding="utf-8")
    today = datetime.date.today()
    (tmp_path / "STATUS.md").write_text(_page(as_of=str(today), live="2000-01-01"), encoding="utf-8")
    assert cs.main(["--root", str(tmp_path)]) == cs.WARN
    assert cs.main(["--release", "--root", str(tmp_path)]) == cs.OK
    (tmp_path / "STATUS.md").write_text(_page(as_of="2000-01-01", live="2000-01-01"), encoding="utf-8")
    assert cs.main(["--release", "--root", str(tmp_path)]) == cs.FAIL


def test_the_command_reads_the_instance_ledger(tmp_path):
    """On an instance tree `main` reads `instance/OPERATIONS.md`, and an unfilled one makes a fresh page WARN. Guards
    the ledger check existing as a function nobody calls — the warning the template and the CHANGELOG promise."""
    (tmp_path / "docker").mkdir()
    (tmp_path / "instance").mkdir()
    (tmp_path / "docker" / "images.lock.yml").write_text(LOCK, encoding="utf-8")
    today = str(datetime.date.today())
    (tmp_path / "STATUS.md").write_text(_page(as_of=today, live=today), encoding="utf-8")
    assert cs.main(["--root", str(tmp_path)]) == cs.OK
    (tmp_path / "instance" / "OPERATIONS.md").write_text("**As of:** YYYY-MM-DD\n", encoding="utf-8")
    assert cs.main(["--root", str(tmp_path)]) == cs.WARN


def test_the_reference_date_is_the_commit_not_the_clock(tmp_path):
    """In a git checkout the reference date is HEAD's committer date, so an age is a function of the commit — a
    re-run of a release's gate gives the answer it gave on the day. Guards the bundle's fallback (today) becoming
    the rule."""
    env = dict(os.environ, GIT_COMMITTER_DATE="2001-02-03T04:05:06+0000", GIT_AUTHOR_DATE="2001-02-03T04:05:06+0000")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "x"], check=True, env=env)
    assert cs.reference_date(str(tmp_path)) == D(2001, 2, 3)
