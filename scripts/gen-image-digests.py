#!/usr/bin/env python3
"""The image digest-lock — the image-world analogue of the collection lockfile's pin+checksum floor (report 22
docs/reviews/2026-06-29-compose-native-install/22-distribution-images.md §4). A tag is mutable; a digest is
content-addressed and Docker verifies it on every pull, so a `@sha256:` pin gives images exactly what L2b gives
collection tarballs: the bytes you recorded are the bytes you run.

Three modes, with a clean offline/network split (the BRICK-1 parallel — no network in the install-gating path):
  * --env  (DEFAULT, OFFLINE): read the committed docker/images.lock.yml and print `<ENV_VAR>=<ref>@<digest>` for
    each kontroll-owned image that HAS a recorded digest. deploy-stack appends these to docker/.env (under
    use_published_images) so the compose fragments pull by digest. An image with no digest (not yet published) is
    omitted → the fragment's `${VAR:-<local-built default>}` falls back to the control-node build. Pure file read.
  * --refresh (DEV/CI, NETWORK): resolve each entry's `ref:tag` → manifest digest (`docker buildx imagetools
    inspect`) and write it back into the lock. Run AFTER a tagged CI publish (or to bump a pin); the resulting lock
    is committed. This is the authoring-time pre-fetch — NEVER the deploy path (a validate test pins that).
    FAILS CLOSED: if any kontroll image's digest cannot be resolved, nothing is written and the exit is 1 — a lock
    must never carry a new ref/tag over an old digest. `--owner <owner>` re-points every kontroll image's ref at
    `ghcr.io/<owner>/<image>` first (the public-split re-pin), so the move is one command, not a hand edit.
  * --check (VALIDATE): schema/format gate — schema:1, each entry has a string ref + tag-or-null + digest that is
    null or a well-formed sha256. Makes NO network call.

Usage:  python3 scripts/gen-image-digests.py [--env | --refresh [tag] [--owner <owner>] | --check]
"""
import os
import re
import subprocess
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK = os.path.join(ROOT, "docker", "images.lock.yml")

# The kontroll-owned images we publish → the .env var each compose fragment reads. C0 = control + vector; C1 adds
# the installer (so docker/services/installer.yaml can pull it by digest instead of building — report 22 §2.1 C).
KONTROLL_IMAGES = {
    "kontroll-control": "KONTROLL_CONTROL_IMAGE",       # the Semaphore runner (semaphore/onboard-gui/api services)
    "kontroll-vector": "KONTROLL_VECTOR_IMAGE",         # the custom Vector image (file_tail_ssh exec-ssh source)
    "kontroll-installer": "KONTROLL_INSTALLER_IMAGE",   # the installer glue (docker/services/installer.yaml)
}
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def load(path=LOCK):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def render(lock):
    """The byte-exact lock content — hand-written so it stays yamllint-clean + diff-minimal across releases."""
    s = ("---\n"
         "# GENERATED/maintained by scripts/gen-image-digests.py — DO NOT hand-edit the digests.\n"
         "# The image-world pin floor (report 22 §4.2): every kontroll-owned image pinned by sha256 digest (the\n"
         "# tag is the human comment; the digest is authoritative, Docker-verified on pull). Recorded by\n"
         "# `gen-image-digests.py --refresh` after a tagged CI publish; `digest: null` ⇒ the deploy falls back to\n"
         "# the local image build (use_published_images stays off until a digest is recorded).\n"
         "schema: 1\n"
         "images:\n")
    for key in sorted(lock.get("images") or {}):
        e = lock["images"][key]
        s += "  %s:\n" % key
        s += "    ref: %s\n" % e["ref"]
        s += "    tag: %s\n" % (("\"%s\"" % e["tag"]) if e.get("tag") else "null")
        s += "    digest: %s\n" % (("\"%s\"" % e["digest"]) if e.get("digest") else "null")
    return s


def emit_env(lock):
    """The OFFLINE deploy seam: VAR=ref@digest for every kontroll image with a recorded digest (others omitted)."""
    out = []
    images = lock.get("images") or {}
    for key, var in KONTROLL_IMAGES.items():
        e = images.get(key) or {}
        if e.get("digest"):
            out.append("%s=%s@%s" % (var, e["ref"], e["digest"]))
    return "\n".join(out)


def _resolve_digest(ref, tag):
    """ref:tag -> the manifest(-list) sha256 via `docker buildx imagetools inspect` (NETWORK; anonymous for a public
    package — the kontroll packages are public; `docker login` only for a private one). Returns the digest or None."""
    try:
        cp = subprocess.run(["docker", "buildx", "imagetools", "inspect", "%s:%s" % (ref, tag)],
                            capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        sys.stderr.write("refresh: cannot inspect %s:%s — %s\n" % (ref, tag, exc))
        return None
    m = re.search(r"^Digest:\s*(sha256:[0-9a-f]{64})", cp.stdout, re.M)
    return m.group(1) if m else None


def refresh(lock, tag_override=None, owner=None):
    """NETWORK: resolve each entry's ref:tag -> digest and write the lock back. tag_override sets every kontroll
    image's tag first (the post-publish flow: `--refresh v1.2.0`); owner re-points every kontroll image's ref at
    ghcr.io/<owner>/<image> (lowercased) first. FAIL CLOSED: an entry whose digest cannot be resolved (no tag, no
    such image, no registry access) leaves the lock UNWRITTEN and returns 1 — the old digest must never be
    committed under a new ref/tag (review 2026-10-09 M4). Returns 0 when every entry was refreshed and written."""
    images = lock.get("images") or {}
    unresolved = []
    for key, e in images.items():
        if owner and key in KONTROLL_IMAGES:
            e["ref"] = "ghcr.io/%s/%s" % (owner.lower(), key)
        if tag_override and key in KONTROLL_IMAGES:
            e["tag"] = tag_override
        if not e.get("tag"):
            sys.stderr.write("refresh: %s has no tag (set one or pass a tag)\n" % key)
            unresolved.append(key)
            continue
        d = _resolve_digest(e["ref"], e["tag"])
        if not d:
            unresolved.append(key)
            continue
        e["digest"] = d
        print("refreshed %s -> %s:%s @ %s" % (key, e["ref"], e["tag"], d))
    if unresolved:
        sys.stderr.write("refresh: unresolved %s — the lock was NOT written (fail closed)\n" % ", ".join(unresolved))
        return 1
    with open(LOCK, "w", encoding="utf-8", newline="\n") as fh:   # LF on every platform (the lock is text=auto eol=lf)
        fh.write(render(lock))
    return 0


def check(lock):
    """Schema/format gate — OFFLINE. Returns a list of problems ([] == valid)."""
    problems = []
    if lock.get("schema") != 1:
        problems.append("schema must be 1")
    images = lock.get("images")
    if not isinstance(images, dict) or not images:
        return problems + ["images: must be a non-empty mapping"]
    for key, e in images.items():
        if not isinstance(e, dict) or not isinstance(e.get("ref"), str):
            problems.append("%s: needs a string ref" % key)
            continue
        if e.get("tag") is not None and not isinstance(e["tag"], str):
            problems.append("%s: tag must be a string or null" % key)
        d = e.get("digest")
        if d is not None and not _SHA256.match(str(d)):
            problems.append("%s: digest %r is not a sha256:<64hex>" % (key, d))
    return problems


def main():
    args = sys.argv[1:]
    if "--check" in args:
        problems = check(load())
        if problems:
            for p in problems:
                sys.stderr.write("images.lock.yml: %s\n" % p)
            sys.exit(1)
        print("gen-image-digests: images.lock.yml well-formed (--check)")
        return
    if "--refresh" in args:
        owner = None
        if "--owner" in args:
            i = args.index("--owner")
            owner = args[i + 1] if i + 1 < len(args) else None
            if not owner or owner.startswith("--"):
                sys.exit("gen-image-digests: --owner needs a value")
            args = args[:i] + args[i + 2:]
        rest = [a for a in args if not a.startswith("--")]
        sys.exit(refresh(load(), rest[0] if rest else None, owner))
    print(emit_env(load()))   # default: --env (offline)


if __name__ == "__main__":
    main()
