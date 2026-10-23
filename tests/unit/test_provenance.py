"""provenance domain — the read-only fleet install-PROVENANCE view (MF-5: no verification theatre).

WHY (the failures these guard):
  * On the 0-signature fleet EVERY collection must classify `unsigned-pinned` (amber) — verified by pin + checksum
    ONLY — so a green "installed" never implies a signature was checked. A collection is `signed` (green) ONLY when
    its policy is `required` AND a keyring is actually held (a declaration and a file: no install reads the policy).
  * `digest_recorded` must reflect the L2b TOFU lock; the read must degrade to `available:false` when the trust
    sidecar isn't generated yet (a fresh tree pre-bootstrap) — never raise (the in-process GUI must survive).
  * the read must NEVER gain a write/actuation path (read-only-by-construction; INVARIANT D*).
"""
import importlib.util
import os

import pytest
import yaml

from _readonly_pins import assert_read_only
from kontroll import paths
from kontroll.service.provenance import fleet_provenance, image_provenance

pytestmark = pytest.mark.unit

_SIDECAR = "ansible/collections/trust.generated.yml"
_LOCK = "instance/trust/observed-digests.yml"
_KEYRING = "instance/trust/galaxy-pubkeys.gpg"


def _write_sidecar(tmp, policy="adaptive"):
    """Write a 2-collection generated trust sidecar (an `==` exact + a `>=` floor) at `policy`, keyring path set."""
    p = tmp / _SIDECAR
    p.parent.mkdir(parents=True, exist_ok=True)
    cols = [("community.docker", "==4.0.0"), ("cisco.ios", ">=8.0.0")]
    body = "---\nschema: 1\ndefault_signature_policy: %s\nkeyring: %s\ncollections:\n" % (policy, _KEYRING)
    for name, ver in cols:
        body += '  - name: %s\n    version: "%s"\n    signature_policy: %s\n    sha256: null\n' % (name, ver, policy)
    p.write_text(body, encoding="utf-8")


def test_reads_the_sidecar_gen_requirements_writes(tmp_path, monkeypatch):
    """fleet_provenance, reading the trust sidecar gen-requirements renders for the example fleet, is available,
    lists one row per collection the sidecar names, in its order, each with the sidecar's pin and policy, and reads
    the sidecar's default policy and its keyring (present once the file it names exists).

    Guards the Supply-chain panel going silently empty, or losing a column, after a key is renamed on one side: the
    other tests here read a sidecar written by hand, so they pass whatever the generator writes (S33)."""
    root = paths.ROOT
    spec = importlib.util.spec_from_file_location("gen_requirements", os.path.join(root, "scripts",
                                                                                   "gen-requirements.py"))
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    with open(os.path.join(root, "instance.example", "fleet.yml"), encoding="utf-8") as fh:
        fleet = yaml.safe_load(fh)
    out, prov = gen.compute(paths.enabled_keys(fleet), os.path.join(root, "actuation"))
    # One collection declares a policy of its own, as an actuation unit's provenance does, so a per-collection key
    # the reader looked for under another name would show — not fall back to the default, which every other carries.
    prov = dict(prov, **{out[0]["name"]: {"signature_policy": "none"}})
    text = gen.render_sidecar(out, prov, fleet.get("signature_policy") or "adaptive",
                              fleet.get("keyring") or gen.DEFAULT_KEYRING)
    written = yaml.safe_load(text)["collections"]
    assert written and written[0]["signature_policy"] == "none", "the sidecar did not carry the declared policy"

    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    (tmp_path / _SIDECAR).parent.mkdir(parents=True)
    (tmp_path / _SIDECAR).write_bytes(text.encode("utf-8"))
    view = fleet_provenance()
    assert view["available"] is True and view["summary"]["total"] == len(written)
    assert [row["name"] for row in view["collections"]] == [c["name"] for c in written]
    for row, c in zip(view["collections"], written):
        assert row["pin"] == c["version"] and row["policy"] == c["signature_policy"], c["name"]
    side = yaml.safe_load(text)
    assert view["default_policy"] == side["default_signature_policy"]
    assert view["keyring_present"] is False                           # the file the sidecar names is not there yet
    assert not os.path.isabs(side["keyring"]), "the example keyring must be repo-relative for this test"
    (tmp_path / side["keyring"]).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / side["keyring"]).write_bytes(b"keyring")
    assert fleet_provenance()["keyring_present"] is True


@pytest.fixture
def prov_root(tmp_path, monkeypatch):
    """Point paths.ROOT at a throwaway tree so fleet_provenance reads the test's sidecar/lock, never the real repo."""
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    return tmp_path


def test_unavailable_when_sidecar_absent(prov_root):
    """No generated sidecar (a fresh tree pre-bootstrap) ⇒ available:false + empty — the GUI degrades, never errors."""
    out = fleet_provenance()
    assert out["available"] is False and out["collections"] == [] and out["summary"]["total"] == 0


def test_all_unsigned_pinned_on_the_zero_sig_fleet(prov_root):
    """The current fleet (adaptive policy, no keyring on disk) classifies EVERY collection `unsigned-pinned` (amber)
    with the honest pin+checksum note — a green 'installed' never implies a signature was checked (MF-5). pin_kind
    distinguishes the `==` exact (closes version-bump too) from the `>=` floor."""
    _write_sidecar(prov_root)                                 # adaptive, keyring path set but the file is absent
    out = fleet_provenance()
    assert out["available"] and out["keyring_present"] is False
    assert out["summary"]["unsigned_pinned"] == 2 and out["summary"]["signed"] == 0
    docker = next(c for c in out["collections"] if c["name"] == "community.docker")
    assert docker["class"] == "unsigned-pinned" and docker["pin_kind"] == "exact" and "no signature" in docker["note"]
    assert next(c for c in out["collections"] if c["name"] == "cisco.ios")["pin_kind"] == "floor"


def test_signed_only_when_required_and_keyring_present(prov_root):
    """A collection is `signed` (green) ONLY when its policy is `required` AND a keyring is actually on disk.
    Required-but-no-keyring stays amber (honest — nothing verifies a signature without a key)."""
    _write_sidecar(prov_root, policy="required")
    assert fleet_provenance()["summary"]["signed"] == 0       # required but the keyring file is absent → still amber
    (prov_root / "instance" / "trust").mkdir(parents=True, exist_ok=True)
    (prov_root / _KEYRING).write_text("x", encoding="utf-8")   # now a keyring is held
    out = fleet_provenance()
    assert out["keyring_present"] and out["summary"]["signed"] == 2 and out["summary"]["unsigned_pinned"] == 0
    assert all(c["class"] == "signed" for c in out["collections"])


def test_digest_recorded_reflects_the_l2b_lock(prov_root):
    """`digest_recorded` is true exactly for a collection L2b has TOFU-bound a sha256 for (matched by `name==`, so a
    floor whose resolved version was recorded also counts). Guards the surface honestly showing what's bound."""
    _write_sidecar(prov_root)
    lock = prov_root / _LOCK
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text('digests:\n  "community.docker==4.0.0": "abc"\n', encoding="utf-8")
    out = fleet_provenance()
    assert next(c for c in out["collections"] if c["name"] == "community.docker")["digest_recorded"] is True
    assert next(c for c in out["collections"] if c["name"] == "cisco.ios")["digest_recorded"] is False
    assert out["summary"]["digests_recorded"] == 1


def test_fleet_provenance_is_read_only():
    """fleet_provenance + its helpers + the GUI route are READ views (the C10 read-only-by-construction contract):
    the AST pin asserts they call no write/actuation verb and open no file for writing — the provenance surface can
    never silently gain a write path."""
    for fn in ("fleet_provenance", "_read_yaml", "_pin_kind"):
        assert_read_only("scripts/kontroll/service/provenance.py", fn)
    assert_read_only("gui/app.py", "api_provenance")


# ── image provenance (C1, report 22 §7.3 — the MF-5 honesty extended to the image supply chain) ──────────────────

def _write_images_lock(tmp, entries):
    """Write a docker/images.lock.yml with {key: digest-or-None}; ref derived, tag set only when a digest is pinned."""
    p = tmp / "docker" / "images.lock.yml"
    p.parent.mkdir(parents=True, exist_ok=True)
    body = "---\nschema: 1\nimages:\n"
    for key, digest in entries.items():
        body += "  %s:\n    ref: ghcr.io/example/%s\n" % (key, key)
        body += "    tag: %s\n" % ('"v1"' if digest else "null")
        body += "    digest: %s\n" % (('"%s"' % digest) if digest else "null")
    p.write_text(body, encoding="utf-8")


def test_image_unavailable_when_lock_absent(prov_root):
    """No docker/images.lock.yml ⇒ available:false + empty — the Index image sub-panel degrades, never errors."""
    out = image_provenance()
    assert out["available"] is False and out["images"] == [] and out["summary"]["total"] == 0


def test_image_classes_digest_pinned_vs_local_build(prov_root):
    """An image with a recorded @sha256: classifies `digest-pinned` (amber — Docker verifies on pull, NOT signed); a
    null-digest image classifies `local-build` (the deploy builds it on the node). `signed` is NEVER true (cosign is
    deferred) — a digest-pin must never read as a verified signature (the MF-5 no-verification-theatre stance, §7.3)."""
    sha = "sha256:" + "a" * 64
    _write_images_lock(prov_root, {"kontroll-control": sha, "kontroll-installer": None})
    out = image_provenance()
    assert out["available"] and out["signing_configured"] is False
    assert out["summary"] == {"total": 2, "digest_pinned": 1, "local_build": 1, "signed": 0}
    ctrl = next(i for i in out["images"] if i["name"] == "kontroll-control")
    assert ctrl["class"] == "digest-pinned" and ctrl["digest_short"] == "a" * 12 and "not a signature" in ctrl["note"]
    inst = next(i for i in out["images"] if i["name"] == "kontroll-installer")
    assert inst["class"] == "local-build" and inst["digest"] is None
    assert all(i["class"] != "signed" for i in out["images"])    # cosign deferred → never green


def test_image_provenance_reads_no_secret_only_public_digests(prov_root):
    """The image lock is image NAMES + PUBLIC @sha256: digests only — image_provenance surfaces exactly those keys and
    never a credential (SEC-2). Guards a future lock field leaking a token/registry password into the read view."""
    _write_images_lock(prov_root, {"kontroll-vector": "sha256:" + "b" * 64})
    row = image_provenance()["images"][0]
    assert set(row) == {"name", "ref", "tag", "digest", "digest_short", "class", "note"}
    assert row["ref"].startswith("ghcr.io/") and "@" not in row["ref"]   # a registry path, not a credential


def test_image_provenance_is_read_only():
    """image_provenance + its GUI route are READ views (read-only-by-construction): the AST pin asserts they call no
    write/actuation verb and open no file for writing — the image-provenance surface can never gain a write path."""
    assert_read_only("scripts/kontroll/service/provenance.py", "image_provenance")
    assert_read_only("gui/app.py", "api_image_provenance")
