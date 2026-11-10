"""scripts/make-bundle.sh — the release bundle must ship the TOOL ONLY, never this instance's private overlay.

Why this guards a real failure: the bundle is a PUBLIC distributable (GitHub Releases / file transfer). If it
carried the `instance/` overlay it would leak this instance's `.sops.yaml` recipients, encrypted secrets,
inventory IPs, fleet, and dashboards to every downloader — and a fresh operator would INHERIT the author's
recipients instead of scaffolding their own via `kontroll-init --fresh` (the blind-Joe footgun). The internal
`docs/reviews/**` snapshots likewise leak IPs/domains. The bundle must instead ship only the public
`instance.example/` stub + the product code. This builds the real bundle and pins that the strip held.
"""
import os
import shutil
import subprocess
import tarfile

import pytest

from kontroll import paths

# make-bundle.sh builds from `git archive HEAD`: every row here reads the tree through git (I-51).
pytestmark = [pytest.mark.integration, pytest.mark.requires_checkout]


def _norm(name):
    """tar -C dir . emits members as ./path — normalise to path (and drop the '.' root)."""
    return name[2:] if name.startswith("./") else name


@pytest.fixture(scope="module")
def bundle_members(tmp_path_factory):
    """Build the real bundle via make-bundle.sh and return its set of member paths. Skips ONLY on non-POSIX
    (a Windows dev box, where passing a `C:\\…` path as $0 mangles the script's `cd "$(dirname "$0")"`); on
    POSIX (CI/Linux) it must build cleanly — a nonzero exit FAILS loudly rather than skipping, so the leak
    guard is never vacuous in CI."""
    if os.name != "posix":
        pytest.skip("make-bundle.sh is POSIX-only (Windows path-mangles $0); the guard is enforced in CI")
    bash = shutil.which("bash") or "/bin/bash"
    out_dir = tmp_path_factory.mktemp("bundle")
    proc = subprocess.run([bash, "scripts/make-bundle.sh", "test0", str(out_dir)],
                          cwd=paths.ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, "make-bundle.sh failed:\n%s\n%s" % (proc.stdout, proc.stderr)
    archive = os.path.join(str(out_dir), "kontroll-test0.tar.gz")
    assert os.path.exists(archive), proc.stdout + proc.stderr
    with tarfile.open(archive) as tf:
        members = {_norm(m.name) for m in tf.getmembers()}
    members.discard(".")
    members.discard("")
    return members


def test_bundle_ships_no_instance_overlay(bundle_members):
    """No member lives under `instance/` (the private overlay: recipients, secrets, inventory IPs, fleet,
    dashboards). One surviving `instance/...` path is a recipient/IP/secret leak in a public artifact.
    `instance.example/` is the public stub and is intentionally NOT matched (no trailing-slash overlap)."""
    leaked = sorted(m for m in bundle_members if m == "instance" or m.startswith("instance/"))
    assert leaked == [], "bundle leaked private overlay paths: %s" % leaked


def test_bundle_ships_no_internal_review_snapshots(bundle_members):
    """No member under `docs/reviews/` — the frozen review snapshots carry this instance's IPs/domains and are
    internal; they must never ride a public bundle (the Phase-5 scrub list)."""
    leaked = sorted(m for m in bundle_members if m.startswith("docs/reviews/"))
    assert leaked == [], "bundle leaked review snapshots: %s" % leaked


def test_bundle_ships_the_example_stub_and_code(bundle_members):
    """The bundle DOES carry the public `instance.example/` stub (so `kontroll-init --fresh` can scaffold) and
    the product code — proving the strip removed only the private overlay, not the tool. A bundle missing the
    stub would make `--fresh` fail (`scaffold_overlay` returns None without instance.example/)."""
    assert "instance.example/.sops.yaml" in bundle_members          # the placeholder-recipient public stub
    assert "scripts/kontroll-init.py" in bundle_members             # the --fresh scaffolder must ship
    assert "scripts/make-bundle.sh" in bundle_members               # sanity: product code is present
