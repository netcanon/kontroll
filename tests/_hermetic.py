"""tests/_hermetic.py — the suite reads the shipped example on EVERY checkout, never an operator's private overlay.

WHY: the same suite runs on a public clone (no `instance/`), on a CI runner, and on a maintainer's tree that
carries a real `instance/` overlay. `kontroll.paths` resolves instance config overlay-first, so on that last tree
dozens of tests — and every service reader a test calls at the real root — were reading private data: their
verdict held only while the private fleet happened to enable the same classes as `instance.example/`, and the
first real fleet edit would have turned named tests red on the private repository alone (the 2026-10-08 review's
rule H7).

HOW: `kontroll.paths` decides the overlay's presence in two probes (`_overlay_file_present`,
`_overlay_dir_present`). `install()` — called by tests/conftest.py at import time, because several test modules
read the fleet at module import, before any fixture runs — wraps both so that they answer "absent" when
`write_root()` is the real checkout. Every reader then falls through to `instance.example/`. A tmp root
(`tmp_repo`, a monkeypatched `paths.ROOT`, `KONTROLL_WRITE_ROOT`) is NOT affected: the overlay-layout tests build
their own `instance/` there and must see it.

There is no production switch for this, on purpose: a setting that can make a live node read the example is a
hazard. It exists only in the test process.

Not affected (deliberate guards OVER the overlay, which read it by its own path, not through the resolver): the
leak guard's instance-token list, the vendor-neutral `group_vars` lint, the topology-leak test, and validate's
`sops-encrypted` step.
"""
import os

from kontroll import paths

REAL_ROOT = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_real_file_present = paths._overlay_file_present
_real_dir_present = paths._overlay_dir_present


def at_real_root():
    """True when the resolver's write root is this checkout itself (not a tmp tree a test built)."""
    return os.path.realpath(paths.write_root()) == REAL_ROOT


def _file_present(overlay):
    return False if at_real_root() else _real_file_present(overlay)


def _dir_present():
    return False if at_real_root() else _real_dir_present()


def install():
    """Wrap the resolver's two overlay probes for this test process. Idempotent."""
    paths._overlay_file_present = _file_present
    paths._overlay_dir_present = _dir_present
