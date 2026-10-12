"""provenance domain — the read-only fleet INSTALL-PROVENANCE view (MF-5: no verification theatre).

`fleet_provenance()` projects the generated trust sidecar (`ansible/collections/trust.generated.yml`, gen-
requirements.py) + the L2b digest lock (`instance/trust/observed-digests.yml`, the install wrapper) into a
per-collection verification posture, so the operator-facing surface can be HONEST about what was actually checked:
on the current public-Galaxy fleet NO signature is served, so every collection is verified by the `==`/floor pin +
the sha256 checksum ONLY — `unsigned-pinned` (amber). A green "installed" must never imply a signature was checked
(docs/reviews/2026-06-25-never-brick-supply-chain/ MF-5). It writes nothing and never gates onboarding (the same
read-only posture as fleet/search; INVARIANT D*) — and reads NO secret (the sidecar carries names + policy + a
sha256 anchor only; SEC-2).
"""
import os

import yaml

from kontroll import paths

_SIDECAR = "ansible/collections/trust.generated.yml"   # the generated trust policy (gen-requirements.py)
_LOCK = "instance/trust/observed-digests.yml"          # the L2b TOFU digest lock (the install wrapper)
_IMAGES_LOCK = "docker/images.lock.yml"                # the C0/C1 image digest-pin lock (gen-image-digests.py)


def _read_yaml(rel):
    """`write_root()/<rel>` parsed, or {} if absent/unreadable — a generated artifact may not exist yet
    (pre-bootstrap). Read from the clone (write_root), NOT the baked read-only ROOT: the trust sidecar + the L2b
    digest lock (instance/trust) + the image lock (docker/) are config-as-data in the canonical, not baked into the
    control image — so a baked api/onboard-gui must read them from the propose clone or the provenance surface
    silently degrades to empty (the latent Rung-1b regression this also fixes). Identity when write_root()==ROOT."""
    try:
        with open(os.path.join(paths.write_root(), rel), encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _pin_kind(version):
    """`exact` for an `==` pin (closes version-bump too), else `floor` (a `>=`/bare spec). PURE."""
    return "exact" if isinstance(version, str) and version.strip().startswith("==") else "floor"


def fleet_provenance():
    """The fleet's per-collection install PROVENANCE (read-only, MF-5). Each collection is classified `signed`
    (green — its declared policy is `required` AND a keyring file is held; a declaration plus a file, not a
    signature this view or any install has checked — SECURITY.md C15-a) or `unsigned-pinned`
    (amber — verified by the pin + sha256 checksum ONLY, no signature; the current 0-signature fleet);
    `digest_recorded` is whether L2b has TOFU-bound a sha256 for it. Returns `{available, default_policy,
    keyring_present, collections, summary}`; `available: false` when the trust sidecar isn't generated yet (a fresh
    tree pre-bootstrap) — the GUI degrades, never errors. READ-ONLY by construction (sidecar + lock reads + a keyring
    file-exists check; no write verb); pinned by test_provenance.test_fleet_provenance_is_read_only. No value/secret
    is read (SEC-2)."""
    side = _read_yaml(_SIDECAR)
    default_policy = side.get("default_signature_policy")
    if not side.get("collections"):
        return {"available": False, "default_policy": default_policy, "keyring_present": False,
                "collections": [], "summary": {"total": 0, "unsigned_pinned": 0, "signed": 0, "digests_recorded": 0}}
    keyring = side.get("keyring")
    keyring_present = bool(keyring) and keyring != "null" \
        and os.path.exists(os.path.join(paths.write_root(), keyring))
    digests = _read_yaml(_LOCK).get("digests") or {}
    cols, n_amber, n_green = [], 0, 0
    for c in side["collections"]:
        name, ver = c.get("name"), c.get("version")
        policy = c.get("signature_policy") or default_policy or "adaptive"
        signed = bool(policy == "required" and keyring_present)
        n_green += int(signed)
        n_amber += int(not signed)
        cols.append({
            "name": name, "pin": ver, "pin_kind": _pin_kind(ver), "policy": policy,
            "class": "signed" if signed else "unsigned-pinned",
            "digest_recorded": any(isinstance(k, str) and k.startswith("%s==" % name) for k in digests),
            "note": ("signature required + keyring held" if signed
                     else "verified by pin + checksum — no signature served by this source")})
    return {"available": True, "default_policy": default_policy, "keyring_present": keyring_present,
            "collections": cols,
            "summary": {"total": len(cols), "unsigned_pinned": n_amber, "signed": n_green,
                        "digests_recorded": sum(1 for c in cols if c["digest_recorded"])}}


def image_provenance():
    """The published-image install PROVENANCE (read-only, the MF-5 honesty extended to the image supply chain —
    report 22 §7.3). Projects `docker/images.lock.yml` into one honest class per kontroll image: `digest-pinned`
    (amber — a recorded `@sha256:` that Docker verifies on every pull, the always-true floor); `local-build` (no
    digest recorded ⇒ the deploy BUILDS this image on the node, no registry trust needed); or `signed` (green — a
    cosign-verified signature). Cosign verification is the DEFERRED rung (report 22 §4.5), so `signed` is never true
    today — a digest-pin is NOT a signature, and the surface must say so (the same no-verification-theatre stance as
    the collection view). Returns `{available, signing_configured, images, summary}`; `available:false` when the lock
    is absent/empty (degrade, never error). READ-ONLY by construction (one lock read, no write verb); reads NO secret
    — the lock is image names + PUBLIC `@sha256:` digests only (SEC-2). Pinned by test_provenance."""
    images_map = _read_yaml(_IMAGES_LOCK).get("images") or {}
    if not images_map:
        return {"available": False, "signing_configured": False, "images": [],
                "summary": {"total": 0, "digest_pinned": 0, "local_build": 0, "signed": 0}}
    signing_configured = False   # cosign verification is the deferred rung (report 22 §4.5) — never green today
    imgs, n_pinned, n_local, n_signed = [], 0, 0, 0
    for key in sorted(images_map):
        e = images_map[key] or {}
        digest = e.get("digest")
        if signing_configured and digest:                       # (inert until cosign lands)
            cls, note = "signed", "cosign-verified signature"
            n_signed += 1
        elif digest:
            cls = "digest-pinned"
            note = "Docker verifies this @sha256: on every pull — not a signature (a digest-pin is not signed)"
            n_pinned += 1
        else:
            cls = "local-build"
            note = "no published digest recorded — the deploy builds this image locally"
            n_local += 1
        imgs.append({"name": key, "ref": e.get("ref"), "tag": e.get("tag"), "digest": digest,
                     "digest_short": (digest.split(":", 1)[1][:12] if isinstance(digest, str) and ":" in digest
                                      else None),
                     "class": cls, "note": note})
    return {"available": True, "signing_configured": signing_configured, "images": imgs,
            "summary": {"total": len(imgs), "digest_pinned": n_pinned, "local_build": n_local, "signed": n_signed}}
