"""paths.write_root() — the Phase-B WRITE-root seam (docs/reviews/2026-06-29-phase-b-baked-code/99-synthesis.md §6).

WHY (the failure these guard): Phase B bakes the immutable code + data registries into the published control image
at a read-only ROOT (`/opt/kontroll`) and points every WRITE at a thin propose clone (`KONTROLL_WRITE_ROOT=/propose`).
Splitting the read-root (ROOT, registries via the import-bound `*_DIR` constants) from the write-root (the clone,
every mutator + git op) is the single highest-C10 mechanism of the bake (review 30 §2): if a mutator follows ROOT
into the baked READ-ONLY image it either crashes on the read-only layer or silently writes a container-writable
overlay that is never pushed — a LOST proposal the GUI reports as "staged" (a correctness AND a C10 trust failure).

These pins assert the seam, with `KONTROLL_WRITE_ROOT==ROOT` as the byte-for-byte identity (review 30 §6.1):
  1. write_root() defaults to ROOT (the zero-behaviour-change identity for the CLI/tests/legacy `/repo` deploy) AND
     follows a repointed `paths.ROOT` (so the tmp_repo fixture + the ~25 ROOT-monkeypatching unit tests still divert);
  2. `KONTROLL_WRITE_ROOT` overrides ROOT (the baked deploy points writes at the clone, never the read-only image);
  3. the instance-overlay seam (resolve/overlay_target/overlay_rel) resolves against write_root while a read-only
     data-registry read (`os.path.join(paths.ROOT, "vectors", …)`) stays on ROOT — the two seams are NON-OVERLAPPING
     (review 30 §2.3). NOTE: `modules/` is the ONE registry that grows with operator action, so post-Rung-2 it is a
     two-root overlay via paths.module_file()/module_keys() (Fork B) — covered by test_module_overlay.py, not here;
  4. a mutator (gitio._write_new) with WRITE_ROOT set writes under the clone, leaving the baked ROOT UNTOUCHED
     (review 30 §6.3 at the unit level — the armed write lands in /propose, the baked image is unmodified).
"""
import os

import pytest

from kontroll import gitio, paths

pytestmark = pytest.mark.unit


def test_write_root_defaults_to_root_and_follows_repoint(monkeypatch):
    """write_root() == ROOT when KONTROLL_WRITE_ROOT is unset (the identity that keeps the CLI/tests/legacy `/repo`
    deploy byte-for-byte), AND follows a repointed paths.ROOT (so the tmp_repo fixture + the ~25 ROOT-monkeypatching
    unit tests still divert writes to the throwaway tree). Guards a frozen write-root that would write the real tree
    under tmp_repo, or break every existing mutator test."""
    monkeypatch.delenv("KONTROLL_WRITE_ROOT", raising=False)
    assert paths.write_root() == paths.ROOT
    monkeypatch.setattr(paths, "ROOT", os.path.join(os.sep, "some", "tmp", "tree"))
    assert paths.write_root() == os.path.join(os.sep, "some", "tmp", "tree")   # dynamic: follows the repoint


def test_write_root_honors_env_over_root(monkeypatch):
    """KONTROLL_WRITE_ROOT overrides ROOT — the BAKED deploy points writes at the propose clone while ROOT is the
    read-only image tree. Guards a write-root that ignored the env and wrote the baked image (the lost-proposal /
    read-only-layer crash this whole seam exists to prevent)."""
    monkeypatch.setattr(paths, "ROOT", os.path.join(os.sep, "opt", "kontroll"))   # the baked read-only image tree
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", os.path.join(os.sep, "propose"))    # the thin clone
    assert paths.write_root() == os.path.join(os.sep, "propose")


def test_overlay_seam_uses_write_root_registries_stay_on_root(tmp_path, monkeypatch):
    """The NON-OVERLAPPING split (review 30 §2.3): an instance-overlay path resolves under write_root (the clone
    holds the live `instance/`), while a data-registry read stays under ROOT (baked). Guards a resolve() that
    diverted a registry read to the clone, or an instance write to the read-only image."""
    baked = tmp_path / "opt" / "kontroll"
    (baked / "vectors").mkdir(parents=True)                         # a baked, read-only (non-growing) data registry
    clone = tmp_path / "propose"
    (clone / "instance").mkdir(parents=True)                        # the clone carries the live instance/ overlay
    (clone / "instance" / "fleet.yml").write_text("fleet: {}\n", encoding="utf-8")
    monkeypatch.setattr(paths, "ROOT", str(baked))
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(clone))

    # instance-overlay seam -> write_root (the clone): the overlay file is found there + resolve joins write_root.
    # normpath so the posix overlay-rel ("instance/fleet.yml") and the OS-native expected compare cross-platform.
    assert paths.overlay_rel("config/fleet.yml") == "instance/fleet.yml"          # existence check hit the clone
    assert os.path.normpath(paths.resolve("config/fleet.yml")) == \
        os.path.normpath(os.path.join(str(clone), "instance", "fleet.yml"))
    assert paths.overlay_target("ansible/secrets/x.sops.yml") == "instance/secrets/x.sops.yml"  # instance/ dir in clone

    # read-only data registry -> ROOT (baked): a direct vectors join follows ROOT, NOT diverted by the overlay seam.
    assert os.path.join(paths.ROOT, "vectors", "telemetry.yml").startswith(str(baked))
    assert not os.path.join(paths.ROOT, "vectors", "telemetry.yml").startswith(str(clone))


def test_mutator_write_targets_clone_leaving_baked_root_untouched(tmp_path, monkeypatch):
    """V1 §6.3 at the unit level: with KONTROLL_WRITE_ROOT set, gitio._write_new writes the instance drop-in under
    the clone (write_root) and the baked ROOT is left UNTOUCHED — the armed write lands in /propose, never the
    read-only image. Guards a mutator that followed ROOT into the baked tree (a crash on the read-only layer, or a
    silent lost proposal)."""
    baked = tmp_path / "opt" / "kontroll"
    baked.mkdir(parents=True)
    clone = tmp_path / "propose"
    clone.mkdir(parents=True)
    monkeypatch.setattr(paths, "ROOT", str(baked))
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(clone))

    gitio._write_new(os.path.join("instance", "inventory", "onboarded-x.yml"), "k: v\n", "# banner\n")

    assert (clone / "instance" / "inventory" / "onboarded-x.yml").exists()       # write landed in the clone
    assert not (baked / "instance").exists()                                     # the baked read-only ROOT is untouched


def test_mutator_write_identity_when_write_root_unset(tmp_path, monkeypatch):
    """With KONTROLL_WRITE_ROOT UNSET, write_root()==ROOT, so a mutator writes under ROOT exactly as today — the
    zero-behaviour-change identity the legacy `/repo` deploy + every existing tmp_repo test relies on. Guards a
    regression where the new seam diverts writes even with the env unset."""
    monkeypatch.delenv("KONTROLL_WRITE_ROOT", raising=False)
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))

    gitio._write_new(os.path.join("instance", "inventory", "onboarded-x.yml"), "k: v\n", "# banner\n")

    assert (tmp_path / "instance" / "inventory" / "onboarded-x.yml").exists()     # write_root()==ROOT==tmp_path


def test_refresh_writes_its_cache_under_the_write_root(tmp_path, monkeypatch):
    """With KONTROLL_WRITE_ROOT set, `service_refresh` writes the capability-matrix cache into the write root and
    returns that path; the image tree gains no file. With it unset the cache is written under ROOT, where it
    always was. Guards `POST /refresh` on a baked install: the cache path was a constant bound to ROOT, which
    that install mounts read-only, so the lowest-risk privileged route failed with a PermissionError."""
    from kontroll import catalog
    from kontroll.service import refresh

    baked, clone = tmp_path / "opt" / "kontroll", tmp_path / "propose"
    baked.mkdir(parents=True)
    clone.mkdir()
    monkeypatch.setattr(catalog, "local_installed", lambda: {})
    monkeypatch.setattr(paths, "ROOT", str(baked))
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(clone))
    out = refresh.service_refresh(vectors=[], overrides=[], backends=[])
    assert out["count"] == 0 and out["path"] == os.path.join(str(clone), "capability-matrix.generated.json")
    assert os.path.isfile(out["path"]) and os.listdir(str(baked)) == []

    monkeypatch.delenv("KONTROLL_WRITE_ROOT", raising=False)
    assert refresh.service_refresh(vectors=[], overrides=[], backends=[])["path"] == os.path.join(
        str(baked), "capability-matrix.generated.json")


def test_the_module_write_target_is_in_the_write_root_and_its_key_is_one_name(tmp_path, monkeypatch):
    """`paths.module_write_path(key)` names `modules/<key>/module.yml` under the WRITE root, with the code root
    elsewhere, and raises for a key that is not one path component. Guards the function checking its own
    argument: every caller today validates the key first, so a later caller that does not would otherwise be
    handed a write target outside `modules/`."""
    baked, clone = tmp_path / "opt" / "kontroll", tmp_path / "propose"
    baked.mkdir(parents=True)
    clone.mkdir()
    monkeypatch.setattr(paths, "ROOT", str(baked))
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(clone))
    assert paths.module_write_path("demo_sw") == os.path.join(os.path.realpath(str(clone)), "modules", "demo_sw",
                                                               "module.yml")
    for key in ("../x", "a/b", "", ".."):
        with pytest.raises(paths.RequestBoundaryError):
            paths.module_write_path(key)


def test_a_confined_file_is_a_regular_file_inside_the_write_root(tmp_path, monkeypatch):
    """`paths.confined_file(rel)` is True for a regular file under the write root and False — never an exception —
    for a directory, a path that is not there, a path that climbs out (also to a file that exists there), an
    absolute path, an empty name and a NUL. Guards the staging clean-up, which asks this for every path of a
    refused request before handing it to `git clean`: a directory there would be removed recursively, and an
    exception there would replace the route's answer with a traceback."""
    root = tmp_path / "propose"
    (root / "instance" / "actuation" / "demo").mkdir(parents=True)
    (root / "instance" / "actuation" / "demo" / "unit.yml").write_bytes(b"key: demo\n")
    (tmp_path / "outside.yml").write_bytes(b"not the service's\n")
    monkeypatch.setattr(paths, "ROOT", str(tmp_path / "opt"))
    monkeypatch.setenv("KONTROLL_WRITE_ROOT", str(root))
    assert paths.confined_file("instance/actuation/demo/unit.yml") is True
    for rel in ("instance/actuation/demo", "instance/actuation/demo/absent.yml", "../outside.yml",
                str(tmp_path / "outside.yml"), str(root / "instance" / "actuation" / "demo" / "unit.yml"), "",
                "instance/\x00unit.yml", None):
        assert paths.confined_file(rel) is False, rel
