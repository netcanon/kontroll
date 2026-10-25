"""A divergent drop-in must raise the CATCHABLE gitio.WriteConflict, never sys.exit.

WHY (the failure this guards): `_write_new` used `sys.exit(...)` to refuse clobbering a divergent file. In the
CLI that's fine, but the GUI/API call the onboard service IN-PROCESS — and `sys.exit` raises SystemExit, which
derives from BaseException, so Flask/Werkzeug does NOT catch it: it killed the onboard-gui request worker and the
browser saw a bare "request failed" with no message. Live-caught onboarding a device with key `cisco_ios` (a
SHIPPED module). The fix raises WriteConflict (a normal Exception) so the routes return a clean 409 and the CLI
exits cleanly. These pins keep it an Exception (not a worker-killer) and keep _write_new raising it.
"""
import os

import pytest

from kontroll import gitio
from kontroll.service import onboard

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_writeconflict_is_a_catchable_exception_not_a_worker_killer():
    """WriteConflict must subclass Exception (caught by `except Exception` in a request handler), and crucially
    must NOT be a SystemExit/BaseException-only — that is the whole point (a SystemExit escapes Flask and kills
    the worker)."""
    assert issubclass(gitio.WriteConflict, Exception)
    assert not issubclass(gitio.WriteConflict, SystemExit)


def test_write_new_raises_writeconflict_on_divergent_existing_file(tmp_path, monkeypatch):
    """A pre-existing file with DIFFERENT content raises WriteConflict (not SystemExit); identical content is a
    no-op (returns False); an absent file writes (returns True). Mirrors the onboard collision a key reuse hits."""
    monkeypatch.setattr(gitio.paths, "ROOT", str(tmp_path))
    rel = "modules/cisco_ios/module.yml"
    os.makedirs(os.path.join(str(tmp_path), "modules", "cisco_ios"))
    with open(os.path.join(str(tmp_path), rel), "w", encoding="utf-8") as fh:
        fh.write("# banner\nEXISTING shipped content\n")
    # divergent -> WriteConflict (catchable), never SystemExit
    with pytest.raises(gitio.WriteConflict):
        gitio._write_new(rel, "DIFFERENT onboard content\n", "# banner\n")
    # identical -> no-op
    assert gitio._write_new(rel, "EXISTING shipped content\n", "# banner\n") is False
    # absent -> writes
    assert gitio._write_new("modules/fresh_key/module.yml", "x\n", "# banner\n") is True
    # overwrite=True -> the GUI 'Overwrite' escape hatch replaces the divergent file (returns True)
    assert gitio._write_new(rel, "DIFFERENT onboard content\n", "# banner\n", overwrite=True) is True
    assert open(os.path.join(str(tmp_path), rel), encoding="utf-8").read() == "# banner\nDIFFERENT onboard content\n"


def test_overwrite_button_and_conflict_flag_are_wired():
    """The GUI offers the Overwrite escape hatch: the route flags a conflict (`conflict: true`) and the template
    renders an `onboard-overwrite` button that re-submits with overwrite=true. Pins the round-trip wiring."""
    app_src = open(os.path.join(ROOT, "gui", "app.py"), encoding="utf-8").read()
    tpl = open(os.path.join(ROOT, "gui", "templates", "index.html"), encoding="utf-8").read()
    assert '"conflict": True' in app_src, "the GUI 409 must flag conflict:true so the front-end shows Overwrite"
    assert "onboard-overwrite" in tpl and "submit(f, collection, backend, true)" in tpl
    assert "overwrite=bool(d.get(\"overwrite\"))" in app_src or "overwrite=bool(d.get('overwrite'))" in app_src


_BODY = "edge_router:\n  hosts:\n    h1:\n      ansible_host: 192.0.2.40\n"


@pytest.mark.parametrize("text, merges", [
    (onboard._INV_BANNER + _BODY, True),                                              # onboard's, or by hand
    ("# a note\n" + onboard._INV_BANNER + _BODY, False),                              # a line put above the banner
    ("\n" + onboard._INV_BANNER + _BODY, False),                                      # a leading blank line
    (onboard._INV_BANNER.replace("remove the host", "drop the host") + _BODY, False),  # the banner line edited
    (onboard._INV_BANNER.replace("\n", " # a note\n", 1) + _BODY, False),           # text after the banner
    ("  " + onboard._INV_BANNER + _BODY, False),                                     # spaces before the banner
], ids=["opens-with-banner", "line-above", "blank-line-above", "banner-edited", "text-after-banner",
        "space-before-banner"])
def test_the_inventory_drop_in_merges_only_when_it_opens_with_the_banner(tmp_path, monkeypatch, text, merges):
    """A drop-in that opens with the onboard banner takes a second host beside the first, whoever wrote it; one that
    does not open with it (a line put above the banner, a leading blank line or spaces, the banner line edited or
    text put after it) is a WriteConflict, however much of the banner it still holds. Guards the rule
    testid_reference.md and the GUI's Overwrite comment state (T18's fold-8 read, N2): a banner test read as
    presence, after whitespace, or on a prefix of the banner passed every suite — and under presence a merge
    silently dropped the line put above; a test on the banner less its newline dropped the text put after it on
    its line, and one after spaces read a tab-led file as YAML and raised (its fold-9 read, N1: G9, G10, G5)."""
    monkeypatch.setattr(gitio.paths, "ROOT", str(tmp_path))
    rel = "ansible/inventory/onboarded-edgeos.yml"
    os.makedirs(os.path.join(str(tmp_path), "ansible", "inventory"))
    with open(os.path.join(str(tmp_path), rel), "w", encoding="utf-8") as fh:
        fh.write(text)
    new = {"h2": {"ansible_host": "192.0.2.50"}}
    if merges:
        assert gitio.write_inventory_host(rel, "edge_router", new, onboard._INV_BANNER) is True
        after = open(os.path.join(str(tmp_path), rel), encoding="utf-8").read()
        assert "h1:" in after and "h2:" in after
    else:
        with pytest.raises(gitio.WriteConflict):
            gitio.write_inventory_host(rel, "edge_router", new, onboard._INV_BANNER)
