"""E2E fixtures — a REAL booted Flask GUI in a daemon thread (no galaxy.py, no ansible, no lab —
search/classify/onboard ALL run the service layer in-process, mocked at their seams), driven by
Playwright headless. Mirrors netcanon's live-server pattern, adapted to Flask (werkzeug's make_server
instead of uvicorn). HTTP Basic creds are supplied via the browser context (http_credentials) — the
Playwright-correct way. The hermetic auth/contract is covered by tests/integration/test_gui_api.py;
this layer proves the rendered UX.
"""
import copy
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "gui"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.environ.setdefault("GUI_USER", "admin")
os.environ.setdefault("GUI_PASSWORD", "test-password-e2e")
os.environ.setdefault("GUI_AUDIT_LOG", os.path.join(tempfile.gettempdir(), "kontroll-e2e-audit.log"))

import _canned  # noqa: E402  (tests/_canned.py — each canned shape has a conformance row, bar E2E_UNIT)
import app as gui  # noqa: E402  (after sys.path is set up)
from kontroll import catalog as _catalog  # noqa: E402  (search/classify run the service layer in-process)
from kontroll import probe as _probe  # noqa: E402

def _fake_deep_probe(coll, version=None):
    """Canned cliconf facts so the in-process classify/deep search renders a cisco.ios card
    (backup=yes → netcommon_cli)."""
    f = _probe._facts(coll, version or "5.0.0", "local", "deep")
    f["plugins"] = {"cliconf": ["ios"]}
    f["modules"] = ["ios_command"]
    return f


def _fake_local_shallow(keywords, limit):
    """Canned SHALLOW facts — the seam the redesigned /api/search uses by DEFAULT (files-only, no ansible-doc).
    Without this the default search would hit the runner's REAL ansible-galaxy and render whatever collections
    happen to be installed. cisco.ios (a cliconf device) by default; a 'proxmox' query returns community.proxmox
    so the e2e can drive the provisioning-surface flow (proxmox is the class that declares a PVEAuditor cred
    prerequisite). Both render as cliconf devices — the offline backend classify needs no real probe. Mirrors
    _fake_deep_probe for the --deep path. Returns a list, like catalog.galaxy_search."""
    # A 'linux' query matches no installed collection — only the Generic SSH host card the backend registry
    # declares (S24) — and a 'nosignal' query returns a collection that carries no signal any backend classifies on,
    # so the card and the "no backend fits" badge render in a real browser.
    if any("linux" in k.lower() for k in keywords):
        return []
    if any("nosignal" in k.lower() for k in keywords):
        f = _probe._facts("acme.nosignal", "1.0.0", "local", "shallow")
        f["plugins"], f["modules"] = {}, ["nosignal_info"]
        return [f]
    coll = "community.proxmox" if any("proxmox" in k.lower() for k in keywords) else "cisco.ios"
    f = _probe._facts(coll, "5.0.0", "local", "shallow")
    f["plugins"] = {"cliconf": ["ios"]}
    f["modules"] = ["ios_command"]
    # A 'xsspaint' query returns a record whose DESCRIPTION carries a script payload — the exact byte a third party
    # controls (probe.shallow_from_galaxy copies `description` verbatim out of the public Galaxy API). It exists so
    # a REAL browser can prove the C19 paint rule holds; the source gate (test_card_paint_gate.py) can only prove
    # the code shape. Keep the payload's `window.__kontroll_xss` marker in sync with the asserting e2e.
    if any("xsspaint" in k.lower() for k in keywords):
        f["description"] = '<img src=x onerror="window.__kontroll_xss=1">'
    return [f]


def _mock_service_seams():
    """Point search/classify's service I/O at offline facts (cisco.ios as a cliconf device, no Galaxy) + register
    one canned actuation unit (the Automations/configure-dialog e2e). Covers the default shallow seam
    (local_shallow), the deep seam (deep_probe), and the actuation registry read (load_actuation_units). Plain
    assignment — the e2e server is a session-scoped thread that can't use function-scoped monkeypatch — so this
    RETURNS the originals and live_server restores them on teardown (and the root conftest orders e2e last),
    keeping a combined `py -m pytest` run from leaking these globals into non-e2e tests."""
    from kontroll.service import provenance as _prov  # local import — the MF-5 read surface seam
    from kontroll.service import discovery as _disc   # local import — the Rung-1b discovery-inbox read seam
    originals = {
        (_catalog, "local_installed"): _catalog.local_installed,
        (_catalog, "local_shallow"): _catalog.local_shallow,
        (_probe, "deep_probe"): _probe.deep_probe,
        (_catalog, "galaxy_search"): _catalog.galaxy_search,
        (_catalog, "load_actuation_units"): _catalog.load_actuation_units,
        (_prov, "fleet_provenance"): _prov.fleet_provenance,
        (_prov, "image_provenance"): _prov.image_provenance,
        (_disc, "read_inbox"): _disc.read_inbox,
    }
    _catalog.local_installed = lambda: dict(_canned.LOCAL_INSTALLED)
    _catalog.local_shallow = _fake_local_shallow
    _probe.deep_probe = _fake_deep_probe
    _catalog.galaxy_search = lambda kw, limit: []
    # One REGISTERED actuation unit, so the Index Automations section and the configure dialog render in a
    # real browser. Mocking the ONE underlying read flows through registered_units_view, configurable_view
    # and build_actuation_plan without overriding those higher seams. The Stage write is not driven (it commits).
    _catalog.load_actuation_units = lambda: [copy.deepcopy(_canned.E2E_UNIT)]
    # MF-5 Supply-chain section: one canned unsigned-pinned collection so the amber badge renders in a real browser.
    # Each seam hands out a COPY: a route that annotated its answer in place would write into the module constant
    # the conformance rows compare, for every later test in the process.
    _prov.fleet_provenance = lambda: copy.deepcopy(_canned.FLEET_PROVENANCE)
    # Rung-1b discovery inbox: two un-onboarded candidates so the panel + the onboard pre-fill render in a real
    # browser — one normal host + one whose hostname is a `<script>` payload (the F6 XSS-safe-render e2e; the ET/
    # textContent rendering must show it as literal text, never execute it). P-1: RFC-5737 IPs + RFC-7042 MACs.
    _disc.read_inbox = lambda: copy.deepcopy(_canned.DISCOVERY_INBOX)
    # C1 images sub-panel (§7.3): one canned digest-pinned image so the amber image badge renders in a real browser.
    _prov.image_provenance = lambda: copy.deepcopy(_canned.IMAGE_PROVENANCE)
    return originals


@pytest.fixture(scope="session")
def live_server():
    """Boot the real Flask app in a daemon thread, the service I/O seams patched. Throwaway process,
    so the patch is a plain assignment, restored on teardown for a clean combined run."""
    from werkzeug.serving import make_server

    _orig_seams = _mock_service_seams()   # search/classify/onboard all run the service layer in-process
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = make_server("127.0.0.1", port, gui.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    for _ in range(100):                       # wait until it answers (a 401 counts — it's up)
        try:
            urllib.request.urlopen(base, timeout=0.5)
            break
        except urllib.error.HTTPError:
            break
        except Exception:
            time.sleep(0.1)
    else:                                      # never came up — fail loudly, not as opaque browser errors
        server.shutdown()
        raise RuntimeError("e2e live server on %s did not respond in ~10s" % base)
    yield base
    server.shutdown()
    for (mod, name), fn in _orig_seams.items():        # restore the globally-assigned seams so a combined
        setattr(mod, name, fn)                          # `py -m pytest` run leaves no fakes behind


@pytest.fixture(scope="session")
def base_url(live_server):
    """pytest-playwright resolves page.goto('/') against this."""
    return live_server


@pytest.fixture
def browser_context_args(browser_context_args, base_url):
    """Supply HTTP Basic creds at the context level (the Playwright-correct way). Use the app's
    ACTUAL loaded creds (gui.GUI_USER/PASSWORD) so this matches whatever the app validates
    against — robust to whichever conftest set GUI_PASSWORD in the env first."""
    return {**browser_context_args, "base_url": base_url,
            "http_credentials": {"username": gui.GUI_USER, "password": gui.GUI_PASSWORD}}
