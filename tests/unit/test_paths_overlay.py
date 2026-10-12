"""The instance-overlay path resolver — kontroll.paths.resolve / overlay_rel.

This is the seam (genericization arc, Decision A) that lets a private, gitignored `instance/` overlay shadow the
shipped generic stubs for a deployment's real config (fleet, inventory, secrets, .sops.yaml). Every Python reader
of an instance path was repointed through it in Phase-2 step (i).

What these verify and why:
  1. With NO overlay file present, every mapped instance path resolves to its LEGACY in-tree location — the
     ZERO-BEHAVIOR-CHANGE invariant the step-(i) repoint depends on. A regression here would silently divert
     instance reads/writes to the wrong tree (or break the live deploy, which has no `instance/` dir yet).
  2. An overlay file, once present, is preferred PER FILE — a half-populated overlay (mid-migration) still falls
     back for the files it lacks, so a partial copy can never half-break the running instance.
  3. A path UNDER a mapped dir maps correctly (ansible/secrets/x -> instance/secrets/x), preserving the
     inventory<->secrets sibling layout the ansible `inventory_dir/../secrets` lookup + SOPS vars-plugin need
     (genericization-manifest.md §4, coupling 2).
  4. A non-instance (tool) path is NEVER rewritten, even when an overlay exists.
  5. overlay_target (the NEW-file WRITE seam) is DIR-active (not file-existence), so a not-yet-written file lands
     in an active overlay AND a write target always equals its git-add path (the C10 staging invariant — the
     silent "encrypt to overlay but stage the legacy path" regression the step-(ii) sweep surfaced).
"""
import os

import pytest

from kontroll import paths


def _touch(root, rel):
    """Create an empty file at root/rel, making parents — to simulate an overlay file being present."""
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").close()
    return p


def test_no_overlay_is_identity_on_mapped_paths(tmp_path, monkeypatch):
    """No instance/ overlay => every mapped instance path resolves to its legacy location (the zero-change
    invariant the whole step-(i) reader repoint relies on)."""
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    for legacy in ("config/fleet.yml", "config/instance.yml", ".sops.yaml",
                   "ansible/inventory", "ansible/secrets",
                   "ansible/secrets/network.sops.yml", "ansible/inventory/hosts.yml"):
        assert paths.overlay_rel(legacy) == legacy
        assert paths.resolve(legacy) == os.path.join(str(tmp_path), legacy)


def test_overlay_file_is_preferred_when_present(tmp_path, monkeypatch):
    """An overlay file shadows the legacy stub; resolve() returns the overlay path."""
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    _touch(str(tmp_path), "instance/fleet.yml")
    assert paths.overlay_rel("config/fleet.yml") == "instance/fleet.yml"
    assert paths.resolve("config/fleet.yml") == os.path.join(str(tmp_path), "instance/fleet.yml")


def test_overlay_fallback_is_per_file(tmp_path, monkeypatch):
    """A half-populated overlay (fleet copied, .sops.yaml not yet) still falls back to the legacy .sops.yaml —
    guards against an all-or-nothing switch that would break a partial migration."""
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    _touch(str(tmp_path), "instance/fleet.yml")
    assert paths.overlay_rel("config/fleet.yml") == "instance/fleet.yml"
    assert paths.overlay_rel(".sops.yaml") == ".sops.yaml"


def test_path_under_mapped_dir(tmp_path, monkeypatch):
    """A file UNDER a mapped dir maps into the overlay when present (ansible/secrets/x -> instance/secrets/x); a
    sibling not yet copied still falls back — preserving per-file safety across the inventory/secrets dirs."""
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    _touch(str(tmp_path), "instance/secrets/network.sops.yml")
    assert paths.overlay_rel("ansible/secrets/network.sops.yml") == "instance/secrets/network.sops.yml"
    assert paths.overlay_rel("ansible/secrets/proxmox.sops.yml") == "ansible/secrets/proxmox.sops.yml"


def test_unmapped_tool_path_passes_through(tmp_path, monkeypatch):
    """A non-instance path (tool code/config) is never rewritten, even with an instance/ overlay present — only
    the explicitly mapped Bucket-B paths are overlay-able."""
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    _touch(str(tmp_path), "instance/fleet.yml")
    assert paths.overlay_rel("config/capture-exceptions.yml") == "config/capture-exceptions.yml"
    assert paths.overlay_rel("modules/_core.yml") == "modules/_core.yml"
    assert paths.resolve("modules/_core.yml") == os.path.join(str(tmp_path), "modules/_core.yml")


def test_overlay_target_falls_back_without_instance_dir(tmp_path, monkeypatch):
    """overlay_target (the NEW-file write seam) returns the legacy path when no instance/ overlay exists — the same
    zero-behavior-change guarantee as resolve(), so the write sites (onboard host, secret domain) stay inert until
    the overlay is stood up in step (ii-c)."""
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    for legacy in ("ansible/inventory/onboarded-x.yml", "ansible/secrets/dashboards.sops.yml", "config/fleet.yml"):
        assert paths.overlay_target(legacy) == legacy


def test_overlay_target_prefers_overlay_when_instance_dir_active(tmp_path, monkeypatch):
    """Once the instance/ dir exists, overlay_target sends a NOT-yet-existent file INTO the overlay — unlike
    overlay_rel (file-existence based), which would keep a brand-new file at the legacy path. This is what lets a
    freshly-onboarded host / new secret domain land in the active overlay rather than a dir nothing reads."""
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    os.makedirs(os.path.join(str(tmp_path), "instance"))   # active overlay; the target files do NOT exist
    assert paths.overlay_target("ansible/inventory/onboarded-x.yml") == "instance/inventory/onboarded-x.yml"
    assert paths.overlay_target("ansible/secrets/newdomain.sops.yml") == "instance/secrets/newdomain.sops.yml"
    # the two seams differ BY DESIGN — overlay_rel will not promote a file that doesn't exist yet:
    assert paths.overlay_rel("ansible/secrets/newdomain.sops.yml") == "ansible/secrets/newdomain.sops.yml"


def test_overlay_target_write_path_equals_git_add_path(tmp_path, monkeypatch):
    """The C10 staging invariant the sweep flagged: a write target and its git-add path MUST agree. Both the writer
    (gitio.sops_write_domain) and the staged-path entry (secrets.build_secret_plan['path'] / onboard inv_path) are
    computed from the SAME overlay_target call, so they are byte-identical whether the file exists yet or not —
    guarding the silent 'encrypt into the overlay but git-add the legacy path → credential never staged' loss."""
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    os.makedirs(os.path.join(str(tmp_path), "instance", "secrets"))
    assert paths.overlay_target("ansible/secrets/dashboards.sops.yml") == "instance/secrets/dashboards.sops.yml"
    # a brand-new (unwritten) domain still agrees — dir-active, not file-existence:
    assert paths.overlay_target("ansible/secrets/brandnew.sops.yml") == "instance/secrets/brandnew.sops.yml"


# --- the shipped-example READ tier (public-checkout floor, genericization Phase 5) --------------------------- #

def _public_checkout(tmp_path, monkeypatch):
    """A tree shaped like a PUBLIC clone: the shipped instance.example/ is present, there is NO instance/ dir and
    NO legacy in-tree stub (the public cut carries neither)."""
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    _touch(str(tmp_path), "instance.example/fleet.yml")
    _touch(str(tmp_path), "instance.example/inventory/hosts.yml")
    _touch(str(tmp_path), "instance.example/.sops.yaml")


def test_public_checkout_reads_the_shipped_example(tmp_path, monkeypatch):
    """On a tree with NO instance/ overlay and NO legacy stub, a READ of a mapped instance path resolves to the
    shipped instance.example/ file — so a public clone / CI checkout runs the generators' --check gates, the
    service readers and the suite against TEST-NET example data instead of dying on a missing fleet/inventory
    (the public-split failure this guards: 60+ collection errors on a checkout without instance/)."""
    _public_checkout(tmp_path, monkeypatch)
    assert paths.resolve("config/fleet.yml") == os.path.join(str(tmp_path), "instance.example/fleet.yml")
    assert paths.resolve("ansible/inventory/hosts.yml") == os.path.join(str(tmp_path), "instance.example/inventory/hosts.yml")
    assert paths.resolve(".sops.yaml") == os.path.join(str(tmp_path), "instance.example/.sops.yaml")


def test_example_tier_never_names_the_staged_path_nor_a_write_target(tmp_path, monkeypatch):
    """The example tier is READ-only by construction: overlay_rel() (the staged/git-add path string) still returns
    the legacy path, and resolve(write=True) — what every writer (keygen .sops.yaml, enable_in_fleet, kontroll-init
    personalize) passes — never points at the shipped placeholder. Guards the silent clobbering of
    instance.example/ by a write on an unconfigured tree."""
    _public_checkout(tmp_path, monkeypatch)
    assert paths.overlay_rel("config/fleet.yml") == "config/fleet.yml"
    assert paths.overlay_rel(".sops.yaml") == ".sops.yaml"
    assert paths.resolve("config/fleet.yml", write=True) == os.path.join(str(tmp_path), "config/fleet.yml")
    assert paths.resolve(".sops.yaml", write=True) == os.path.join(str(tmp_path), ".sops.yaml")


def test_example_tier_is_off_once_an_instance_dir_exists(tmp_path, monkeypatch):
    """A CONFIGURED node (an instance/ dir exists) that is missing a file keeps failing loud at the legacy path —
    the example tier must never paper over a lost overlay file with placeholder data on a real deployment."""
    _public_checkout(tmp_path, monkeypatch)
    os.makedirs(os.path.join(str(tmp_path), "instance"))          # configured (dir-active), but fleet.yml absent
    assert paths.resolve("config/fleet.yml") == os.path.join(str(tmp_path), "config/fleet.yml")


def test_example_tier_prefers_a_legacy_stub_and_ignores_unmapped_paths(tmp_path, monkeypatch):
    """A legacy in-tree stub still wins over the example (the pre-overlay zero-change invariant), and a non-instance
    (tool) path is never redirected into instance.example/ even on a public checkout."""
    _public_checkout(tmp_path, monkeypatch)
    _touch(str(tmp_path), "config/fleet.yml")
    assert paths.resolve("config/fleet.yml") == os.path.join(str(tmp_path), "config/fleet.yml")
    assert paths.resolve("modules/_core.yml") == os.path.join(str(tmp_path), "modules/_core.yml")


def test_the_overlay_probe_answers_absent_for_a_path_that_leaves_the_write_root(tmp_path, monkeypatch):
    """A mapped instance path that climbs out of the write root with `..` is "absent" to the overlay probe even
    when a file really exists where it points, so overlay_rel() does not hand back an overlay location outside the
    tree; an ordinary overlay file, and one reached through a `..` that stays inside, are still found. Guards the
    join inside the probe: names are validated at the request seam (SECURITY.md C22), and this is the same rule
    where the path is built — it used to ask the filesystem about wherever the joined string pointed."""
    root = tmp_path / "tree"
    _touch(str(root), "instance/secrets/network.sops.yml")
    _touch(str(tmp_path), "outside.yml")
    monkeypatch.setattr(paths, "ROOT", str(root))
    monkeypatch.delenv("KONTROLL_WRITE_ROOT", raising=False)
    climbing = "ansible/secrets/../../../outside.yml"
    assert os.path.exists(os.path.join(str(root), "instance/secrets/../../../outside.yml"))    # the file IS there
    assert paths.overlay_rel(climbing) == climbing                  # ...and the probe does not answer "present"
    assert paths.overlay_rel("ansible/secrets/network.sops.yml") == "instance/secrets/network.sops.yml"
    assert paths.overlay_rel("ansible/secrets/../secrets/network.sops.yml") == \
        "instance/secrets/../secrets/network.sops.yml"


def test_the_overlay_probe_follows_a_symlinked_instance_directory(tmp_path, monkeypatch):
    """An `instance/` that is a symlink to a directory OUTSIDE the tree is still read as the overlay. Guards the
    confinement above being written with realpath(): that would resolve the link, find the target outside the
    write root, and silently make an operator's relocated overlay "absent" — every reader falling back to the
    legacy stubs on a configured node."""
    root, elsewhere = tmp_path / "tree", tmp_path / "kept-elsewhere"
    root.mkdir()
    _touch(str(elsewhere), "fleet.yml")
    try:
        os.symlink(str(elsewhere), str(root / "instance"), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform or account cannot create a directory symlink")
    monkeypatch.setattr(paths, "ROOT", str(root))
    monkeypatch.delenv("KONTROLL_WRITE_ROOT", raising=False)
    assert paths.overlay_rel("config/fleet.yml") == "instance/fleet.yml"
