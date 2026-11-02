"""homepage — the read+stage "arrange the portal" service for the GUI Homepage editor (#136, Phase 2).

These pin service/homepage.py: read_board() parses the two overlay files into a normalized board; the LINE-SPAN
serializer re-emits the board comment- and fleet-marker-preserving (never yaml.safe_dump); build_homepage_plan
diffs + tokens it; promote_board token-gates the write. WHY (the gap this fills): gethomepage only READS YAML, so
a GUI-configurable portal means the :8443 GUI rewrites the overlay — and a rewrite that dropped the operator's
comments or corrupted the file would be a config-eating bug. The round-trip-IDENTITY test is the load-bearing
safety net: an unchanged board must re-emit byte-for-byte. They also pin the security invariants: the serializer
can only relocate/omit existing spans (never synthesize a tile — C11), and read_board is read-only-by-construction.
"""
import logging

import pytest

from _readonly_pins import assert_read_only
from kontroll.service import homepage

pytestmark = pytest.mark.unit

_SERVICES = (
    "# Homepage tiles (test fixture).\n"
    "- Control plane:\n"
    "    - GUI:\n"
    "        href: https://192.0.2.9:8443\n"
    "        description: onboarding\n"
    "    - Semaphore:\n"
    "        href: http://192.0.2.9:3001\n"
    "        description: ansible automation\n"
    "\n"
    "- Infrastructure:\n"
    "    - Portal:\n"
    "        href: https://control.example.test\n"
    "        description: npm portal\n"
)
_SETTINGS = (
    "title: kontroll — test\n"
    "theme: dark\n"
    "layout:\n"
    "  Control plane:\n"
    "    style: row\n"
    "    columns: 4\n"
    "  Infrastructure:\n"
    "    style: row\n"
    "    columns: 4\n"
)


@pytest.fixture
def board_repo(tmp_repo):
    """tmp_repo seeded with a homepage overlay (services.yaml + settings.yaml) at the literal homepage path."""
    d = tmp_repo / "instance" / "dashboards" / "homepage"
    d.mkdir(parents=True)
    (d / "services.yaml").write_text(_SERVICES, encoding="utf-8")
    (d / "settings.yaml").write_text(_SETTINGS, encoding="utf-8")
    return tmp_repo


def test_read_board_parses_sections_tiles_and_order(board_repo):
    """read_board returns the two sections in settings.yaml layout order, each with its tiles in services.yaml
    order, present:true, has_widget:false. Guards the normalized board the editor renders + the order it reorders."""
    b = homepage.read_board()
    assert [s["name"] for s in b["sections"]] == ["Control plane", "Infrastructure"]
    cp = b["sections"][0]
    assert [t["name"] for t in cp["items"]] == ["GUI", "Semaphore"]
    assert cp["items"][0] == {"name": "GUI", "href": "https://192.0.2.9:8443", "description": "onboarding",
                              "has_widget": False, "widget_type": None, "present": True}
    assert b["title"] == "kontroll — test"


def test_round_trip_identity_unchanged_board_reemits_byte_for_byte(board_repo):
    """THE load-bearing safety net: planning an UNCHANGED board re-emits both files byte-for-byte (comments +
    blank lines + structure preserved) and reports no change / no diff. Guards the config-eating bug a YAML
    re-dump would cause — the line-span serializer must be an identity on a no-op."""
    plan = homepage.build_homepage_plan(homepage.read_board())
    assert plan["error"] is None
    assert plan["text_after"]["services"] == _SERVICES        # byte-for-byte
    assert plan["text_after"]["settings"] == _SETTINGS
    assert plan["summary"]["changed"] is False and plan["diff"] == []


def test_reorder_tiles_within_a_section(board_repo):
    """Swapping two tiles' order in the board re-emits services.yaml with the tiles in the NEW order (Semaphore
    before GUI), comments preserved, settings untouched. Guards the intra-section reorder the operator drives."""
    b = homepage.read_board()
    b["sections"][0]["items"].reverse()                        # Semaphore, GUI
    svc = homepage.build_homepage_plan(b)["text_after"]["services"]
    assert svc.index("- Semaphore:") < svc.index("- GUI:")     # the order flipped
    assert "# Homepage tiles (test fixture)." in svc           # the header comment survived


def test_deselect_omits_the_tile_span_and_reports_removal(board_repo):
    """Marking a tile present:false drops its whole span from services.yaml (and only it) and surfaces it in
    summary.removed_tiles. Guards the membership-selection write + the 'here's what will be removed' summary the
    confirm keys off."""
    b = homepage.read_board()
    b["sections"][0]["items"][1]["present"] = False            # drop Semaphore
    plan = homepage.build_homepage_plan(b)
    svc = plan["text_after"]["services"]
    assert "- Semaphore:" not in svc and "ansible automation" not in svc
    assert "- GUI:" in svc                                     # the kept tile + its body remain
    assert plan["summary"]["removed_tiles"] == ["Semaphore"]


def test_reorder_sections_reorders_both_files(board_repo):
    """Putting Infrastructure before Control plane re-emits services.yaml group order AND settings.yaml layout
    key-order to match (the two order surfaces stay in sync). Guards the section reorder."""
    b = homepage.read_board()
    b["sections"].reverse()                                    # Infrastructure, Control plane
    plan = homepage.build_homepage_plan(b)
    svc, sett = plan["text_after"]["services"], plan["text_after"]["settings"]
    assert svc.index("- Infrastructure:") < svc.index("- Control plane:")
    assert sett.index("Infrastructure:") < sett.index("Control plane:")


def test_validate_rejects_an_unknown_section_or_tile(board_repo):
    """A board that names a section or tile NOT in the current file is rejected (the serializer never synthesizes
    a tile — C11 / config-injection guard). Guards a forged board smuggling a new href/tile into the portal."""
    b = homepage.read_board()
    b["sections"][0]["items"].append({"name": "Evil", "href": "http://x", "present": True})
    assert "validation" in (homepage.build_homepage_plan(b)["error"] or "")
    b2 = homepage.read_board()
    b2["sections"].append({"name": "Phantom", "items": [], "generated": False})
    assert "validation" in (homepage.build_homepage_plan(b2)["error"] or "")


def test_apply_is_idempotent(board_repo):
    """Applying a plan writes the files once; a second apply of the same plan reports changed:False (no write) —
    the idempotent-apply contract (a re-apply must report 0 changed, like every kontroll write path)."""
    b = homepage.read_board()
    b["sections"][0]["items"].reverse()
    plan = homepage.build_homepage_plan(b)
    first = homepage.apply_homepage_plan(plan)
    assert first["error"] is None and first["changed"] is True
    assert homepage.apply_homepage_plan(plan)["changed"] is False   # second apply: no change


def test_promote_board_refuses_a_drifted_token(board_repo):
    """promote_board with a stale/wrong token refuses with error 'drift' (the route returns 409) — the anti-drift
    gate: the approved bytes must still match at apply. Guards a board that changed between propose and promote
    from silently writing the wrong thing."""
    b = homepage.read_board()
    b["sections"][0]["items"].reverse()
    assert homepage.promote_board(b, "not-the-real-token")["error"] == "drift"


def test_a_write_failure_returns_a_code_and_logs_no_exception_text(board_repo, tmp_path, monkeypatch, caplog):
    """An overlay write that fails (here the file's directory is gone, so the OSError's text holds an absolute path)
    returns the code `write_failed` with `changed` False, and logs one line naming the file, the exception's type and
    its errno — never the OSError's text. Guards brief 16 §9.1's class C (T20): the service returned
    `"write failed: %s" % e`, and the GUI route put that path inside the container in the browser's 422."""
    b = homepage.read_board()
    b["sections"][0]["items"].reverse()
    plan = homepage.build_homepage_plan(b)
    gone = tmp_path / "t20-leak-sentinel-dir"                        # the write target's directory, never made
    monkeypatch.setattr(homepage, "_hp_path", lambda name: str(gone / name))
    with caplog.at_level(logging.ERROR, logger="kontroll.service.homepage"):
        out = homepage.apply_homepage_plan(plan)
    assert out["error"] == "write_failed" and out["changed"] is False
    lines = [rec.getMessage() for rec in caplog.records if rec.name == "kontroll.service.homepage"]
    assert lines == ["homepage write failed: file=services.yaml type=FileNotFoundError errno=2"], lines
    assert all("t20-leak-sentinel" not in rec.getMessage() for rec in caplog.records)


def test_homepage_read_board_is_read_only_by_construction():
    """read_board is pure-read: the shared AST pin (#133) asserts it calls no write/actuation verb nor opens a
    file for writing. Guards the board READ ever gaining a write path (the writes are apply_homepage_plan /
    promote_board, which the pin now lists in WRITE_VERBS — P0b)."""
    assert_read_only("scripts/kontroll/service/homepage.py", "read_board")


def test_splice_fleet_block_appends_then_replaces_preserving_operator_tiles():
    """splice_fleet_block (the gen-homepage-side inverse of the editor's reorder, #130) APPENDS the fleet block
    when absent and REPLACES it in place on a re-run, preserving every operator tile + comment outside the markers
    byte-for-byte. The generator and this editor share ONE definition of the span (this function + _split_services),
    so they can't drift on the boundary. Guards a splice that clobbers operator content or stacks duplicate blocks."""
    blk1 = (homepage.FLEET_BEGIN + "\n- Fleet:\n    - core_switch · sw1:\n        href: https://192.0.2.2\n"
            "        description: cisco_ios\n" + homepage.FLEET_END + "\n")
    appended = homepage.splice_fleet_block(_SERVICES, blk1)
    assert appended.startswith(_SERVICES)                          # operator content untouched; block added after it
    assert "- Fleet:" in appended and "core_switch · sw1" in appended
    blk2 = (homepage.FLEET_BEGIN + "\n- Fleet:\n    - edge_firewall · fw1:\n        href: https://192.0.2.1\n"
            "        description: fortigate\n" + homepage.FLEET_END + "\n")
    replaced = homepage.splice_fleet_block(appended, blk2)
    assert "core_switch · sw1" not in replaced                     # the old span was replaced, not duplicated
    assert "edge_firewall · fw1" in replaced and replaced.count(homepage.FLEET_BEGIN) == 1
    assert "- Control plane:" in replaced and "- Semaphore:" in replaced   # operator tiles preserved


# --- the shipped-example READ tier (public split, 2026-10-08) ------------------------------------------------- #

def test_read_board_falls_back_to_the_shipped_example_on_an_unconfigured_tree(tmp_repo):
    """On a tree with NO instance/ overlay at all (a public clone / CI checkout / a node before kontroll-init --fresh),
    read_board() renders the SHIPPED instance.example/ board instead of an empty panel — the failure the first
    GitHub-hosted e2e run of the public cut hit (the Homepage editor opened with zero sections). The tier is
    READ-only: apply_homepage_plan on that tree fails closed (the overlay dir does not exist: the code
    `write_failed`) and the example files are byte-identical afterwards — the shipped example can never become a
    write target."""
    ex = tmp_repo / "instance.example" / "dashboards" / "homepage"
    ex.mkdir(parents=True)
    (ex / "services.yaml").write_text(_SERVICES, encoding="utf-8")
    (ex / "settings.yaml").write_text(_SETTINGS, encoding="utf-8")
    assert not (tmp_repo / "instance").exists()                      # unconfigured: no overlay dir at all
    b = homepage.read_board()
    assert [s["name"] for s in b["sections"]] == ["Control plane", "Infrastructure"]
    assert b["title"] == "kontroll — test"
    plan = homepage.build_homepage_plan(b)
    assert plan["error"] is None
    out = homepage.apply_homepage_plan(plan)                         # a WRITE on the unconfigured tree
    assert out["error"] in (None, "write_failed")
    assert not (tmp_repo / "instance" / "dashboards" / "homepage" / "services.yaml").exists() or out["error"] is None
    assert (ex / "services.yaml").read_text(encoding="utf-8") == _SERVICES   # the example is never written
    assert (ex / "settings.yaml").read_text(encoding="utf-8") == _SETTINGS


def test_read_board_prefers_the_overlay_once_an_instance_dir_exists(tmp_repo):
    """Once an instance/ dir exists (a configured node), the example tier is OFF even for a file the overlay lacks:
    a missing overlay board degrades to the empty board exactly as before, never to placeholder tiles."""
    ex = tmp_repo / "instance.example" / "dashboards" / "homepage"
    ex.mkdir(parents=True)
    (ex / "services.yaml").write_text(_SERVICES, encoding="utf-8")
    (ex / "settings.yaml").write_text(_SETTINGS, encoding="utf-8")
    (tmp_repo / "instance").mkdir()                                   # configured, board files absent
    assert homepage.read_board()["sections"] == []
