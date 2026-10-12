"""audit domain — declared (enabled-module collections) vs installed.

service_audit returns {"enabled","declared","installed"}: the enabled module keys, the
sorted collections those modules declare, and the installed-collection map. The CLI
marks each declared collection installed/MISSING (key presence, so an installed-but-
version-None collection still reads as installed — matching the original).
"""
import os

import yaml

from kontroll import catalog, paths


def service_audit():
    """Return {"enabled": [module keys], "declared": [sorted collection names from those
    modules], "installed": {collection: version}} — pure reads of instance/fleet.yml +
    modules/*/module.yml + `ansible-galaxy collection list`."""
    fleet = yaml.safe_load(open(paths.resolve("config/fleet.yml"), encoding="utf-8")) or {}
    enabled = paths.enabled_keys(fleet)
    declared = set()
    for key in enabled:
        mp = paths.module_file(key)
        if os.path.exists(mp):
            m = yaml.safe_load(open(mp, encoding="utf-8")) or {}
            for c in m.get("collections", []):
                declared.add(c["name"])
    return {"enabled": enabled, "declared": sorted(declared), "installed": catalog.local_installed()}
