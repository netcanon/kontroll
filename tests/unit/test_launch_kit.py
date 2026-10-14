"""C1 bundle-as-compose guards (report 22 §6 — docs/reviews/2026-06-29-compose-native-install/22-distribution-images.md).

WHY — C1 lets a fresh control node install from PUBLISHED, digest-pinned images with NO git clone and NO `docker build`:
`scripts/make-launch-kit.sh` assembles the tiny launch kit (instance-stripped tree + digest pins + the launcher), and
`scripts/kontroll.sh` (shipped as the kit's `kontroll`) pulls the installer by digest and then runs it with a plain
`run` (the image is present, so nothing is built). Three
properties must hold or the distribution either leaks the lab's topology or silently rebuilds: (1) the launcher uses the
PUBLISHED seam (a host `docker pull`, then `run`; reads images.env, forces use_published_images for init/check); (2) the builder STRIPS
the private instance/ overlay + review snapshots (the make-bundle.sh discipline, image-world #72 §7.2); (3) a real
assembly produces the launcher + images.env + the public catalog and ZERO instance/ leak. Each test names its failure.
"""
import os
import shutil
import subprocess
import sys
import tarfile

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def test_launcher_host_pre_pulls_and_runs_without_no_build():
    """scripts/kontroll.sh (the bundle launcher) must PRE-PULL the published images on the HOST (where the operator is
    docker-login'd) so deploy-stack's in-container `docker compose up` finds them present — the installer container
    runs as root with NO ghcr creds, so an in-container pull 401s (dogfood-caught 2026-06-29). It must run the installer
    with a plain `run --rm init` (NOT `--no-build`, which isn't a valid `compose run` flag on all versions; the host
    pull guarantees presence so no build fires). And read images.env + force use_published_images + pass MF-1."""
    s = _read("scripts/kontroll.sh")
    assert "docker pull" in s, "must pre-pull the published images on the host (in-container pull has no creds → 401)"
    assert "run --rm init" in s, "must run the installer with a plain `run --rm init` (no --no-build flag)"
    assert "images.env" in s, "must read the rendered digest pins from images.env (no host python)"
    assert "use_published_images=true" in s, "init/check must force the published-image deploy path"
    assert "KONTROLL_OPERATOR_UID" in s and "KONTROLL_REPO_ROOT" in s, "must pass the MF-1 operator identity"


def test_launcher_git_inits_the_kit_tree():
    """A launch kit is a tarball extract with NO .git, but local-canonical seeds the C10 canonical from the working
    tree's git (it `git remote add local` + pushes). So scripts/kontroll.sh must git-init the kit + commit the current
    tree (incl. a freshly-scaffolded instance/) — the step scripts/install.sh does for the bundle path. Guards the
    `not a git repository` failure the dogfood hit (2026-06-29)."""
    s = _read("scripts/kontroll.sh")
    assert 'git -C "$ROOT" init' in s, "must git-init the kit working tree (local-canonical seeds the canonical from it)"
    assert 'git -C "$ROOT" add -A' in s and "commit" in s, "must capture the current tree so the canonical seed has a HEAD"


def test_launcher_tolerates_crlf_images_env():
    """The launcher must strip a trailing \\r when sourcing images.env (a Windows-built kit's pins carry CRLF, which
    yields `invalid reference format` on pull — dogfood-caught). Defence-in-depth alongside make-launch-kit's strip."""
    s = _read("scripts/kontroll.sh")
    assert "tr -d '\\r'" in s, "must strip CR from the image refs (CRLF-tolerant images.env read)"


def test_launch_kit_builder_strips_instance_and_ships_pins():
    """scripts/make-launch-kit.sh must strip the private instance/ overlay + docs/reviews (the make-bundle.sh strip,
    image-world #72 §7.2), render the digest pins to images.env, and ship the launcher as `kontroll`. It must REFUSE
    to build an unpinned kit by default (a kit with no recorded digests can't pull) unless --allow-unpinned. Guards a
    kit that leaks instance topology, or ships unrunnable/unpinned without saying so."""
    s = _read("scripts/make-launch-kit.sh")
    assert 'rm -rf "$dst"/instance "$dst"/docs/reviews' in s, "must strip the private instance/ overlay + reviews"
    assert "gen-image-digests.py --env" in s, "must render the digest pins from the lock"
    assert 'cp scripts/kontroll.sh "$dst/kontroll"' in s, "must ship the launcher as the kit's kontroll"
    assert "allow-unpinned" in s, "must guard the unpinned path behind an explicit flag"


def test_airgap_builder_pulls_by_digest_saves_and_kits():
    """scripts/make-bundle-airgap.sh must (report 22 §6.3): pull every kontroll image BY DIGEST for the target arch,
    `docker save` them into one tar, and bundle the launch kit alongside — so an air-gapped node installs with zero
    registry reachability. It must REQUIRE recorded digests (no local build on a disconnected node). Guards an air-gap
    bundle that silently ships unpinned, omits the save tar, or forgets the kit."""
    s = _read("scripts/make-bundle-airgap.sh")
    assert "gen-image-digests.py --env" in s, "must read the pinned image refs from the lock"
    assert "docker pull --platform" in s, "must pull each image by digest for the target arch"
    assert "docker save -o" in s, "must docker-save the images into one tar"
    assert "make-launch-kit.sh" in s, "must bundle the launch kit alongside the images tar"
    assert "has no recorded digests" in s, "must refuse an unpinned air-gap bundle (no local build off-grid)"


def test_launcher_is_airgap_tolerant():
    """scripts/kontroll.sh must SKIP the registry pull when the installer image is already present locally — the
    air-gap case where it was `docker load`ed from the bundle (no registry reachable). Guards a launcher that hard-
    fails off-grid by always pulling. Online (image absent) it still pulls + digest-verifies."""
    s = _read("scripts/kontroll.sh")
    assert 'docker image inspect "$_img"' in s, "must check local presence before pulling each image"
    assert "already present" in s, "must skip the pull when the image is already loaded (air-gap)"


def test_make_launch_kit_strips_crlf_and_deploy_check_runs_digest_read():
    """Two dogfood fixes pinned together: (1) make-launch-kit.sh must `tr -d '\\r'` the rendered images.env so a
    Windows build host's CRLF doesn't poison the refs; (2) deploy-stack's digest-read command must carry
    `check_mode: false` so `kontroll check` (a --check dry-run) reads the lock — else the fail-closed assert wrongly
    fires 'no recorded digest' under the published path. Both caught on the 2026-06-29 fresh-VM verify."""
    mk = _read("scripts/make-launch-kit.sh")
    assert "--env | tr -d '\\r'" in mk, "make-launch-kit must strip CR from the rendered images.env"
    # The kit must ship the WORKING-TREE lock (the same source images.env is rendered from), not git-archive HEAD's —
    # else an uncommitted `--refresh` yields a kit with a pinned images.env but a null lock, and deploy-stack (which
    # reads the LOCK) fails 'no recorded digest' at install (Rung-3 dogfood-caught 2026-06-29).
    assert 'cp docker/images.lock.yml "$dst/docker/images.lock.yml"' in mk, \
        "make-launch-kit must ship the working-tree images.lock.yml so the kit's lock + images.env agree"
    ds = _read("ansible/playbooks/deploy-stack.yml")
    assert "check_mode: false" in ds, "deploy-stack's digest-read must run under --check so the assert sees the digests"


# --- Phase B Rung 3: the canonical/kit shrink (the baked code is dropped; the canonical-run closure stays) --------

# The promote/FIX-M9/deploy-stack closure that runs from the CANONICAL CLONE (a non-baked path), so the kit must
# KEEP it. The C10 promote slice + FIX-M9's generator; verified by the closure analysis
# (docs/reviews/2026-06-29-phase-b-baked-code/99-synthesis.md §1). gen-requirements.py is the one the synthesis's
# original "4-file slice" missed — the promote gate runs THIS checkout's copy of it over the proposal's files, and
# absent it the never-brick FIX-M9 gate cannot run (it refuses, and says so).
_PROMOTE_SLICE = ("scripts/kontroll-promote.py", "scripts/gen-requirements.py",
                  "scripts/kontroll/__init__.py", "scripts/kontroll/paths.py", "scripts/kontroll/gitio.py",
                  "scripts/kontroll/canon.py",          # the gate reads the proposal's commit through it
                  "scripts/kontroll/yamlscalar.py")     # imported by gen-requirements.py (the sidecar's keyring)


def test_the_promote_slice_names_everything_the_promote_gate_imports():
    """Every `kontroll.*` module that scripts/gen-requirements.py and scripts/kontroll-promote.py import — the two
    scripts the promote gate runs from the canonical clone — is listed in `_PROMOTE_SLICE`, and every listed file
    exists. Guards the slice going stale the day one of them gains an import: the list is what the next test
    protects from being stripped out of the launch kit, and a module that is imported and not listed could be
    stripped with every test green — the promote gate would then fail to start on a kit-installed node."""
    import ast
    roots = ["scripts/gen-requirements.py", "scripts/kontroll-promote.py"]
    imported, queue = set(), list(roots)
    while queue:                                              # transitively: a listed module's own imports count
        script = queue.pop()
        for node in ast.walk(ast.parse(_read(script))):
            found = set()
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "kontroll":
                base = "scripts/" + node.module.replace(".", "/")
                for alias in node.names:
                    candidate = "%s/%s.py" % (base, alias.name)
                    found.add(candidate if os.path.exists(os.path.join(ROOT, candidate)) else base + ".py")
            elif isinstance(node, ast.Import):
                found = {"scripts/" + a.name.replace(".", "/") + ".py" for a in node.names
                         if a.name.split(".")[0] == "kontroll"}
            for path in found - imported:
                imported.add(path)
                queue.append(path)
    assert "scripts/kontroll/yamlscalar.py" in imported and "scripts/kontroll/gitio.py" in imported, \
        "the scan is not reading the scripts' imports"
    assert sorted(imported - set(_PROMOTE_SLICE)) == []
    assert sorted(set(_PROMOTE_SLICE) - imported - set(roots) - {"scripts/kontroll/__init__.py"}) == []
    assert [f for f in _PROMOTE_SLICE if not os.path.exists(os.path.join(ROOT, f))] == []


def test_launch_kit_strips_baked_code_but_keeps_the_canonical_closure():
    """Rung 3: make-launch-kit.sh must STRIP the baked code (api/ gui/ — they run from the published control image at
    /opt/kontroll, not the kit-seeded canonical) + the never-installed dev trees (tests/ .github/ .claude/), while
    KEEPING the closure that runs from the canonical/kit: scripts/ WHOLE (the deploy-stack generators + the C10
    promote slice + FIX-M9's gen-requirements), ansible/, and the data registries. Guards (a) shipping api/gui source
    a minimized kit can't run (and would tempt a broken local build), and (b) over-stripping scripts/ — which would
    silently break deploy-stack's regen or fail-open the FIX-M9 gate."""
    s = _read("scripts/make-launch-kit.sh")
    for d in ("api", "gui", "tests"):
        assert '"$dst"/%s' % d in s, "make-launch-kit must strip the baked/dev dir %s/ (it never runs from the kit)" % d
    # the canonical-run closure must NOT be in the strip list
    for keep in ("scripts", "ansible", "modules"):
        assert '"$dst"/%s ' % keep not in s and '"$dst"/%s\n' % keep not in s, \
            "make-launch-kit must NOT strip %s/ (it runs from the canonical/kit — promote/FIX-M9/deploy-stack)" % keep


def test_launcher_forces_baked_code_for_the_minimized_kit():
    """A minimized kit has NO api/gui source, so the services CANNOT run from the /repo clone — the `kontroll` launcher
    must force `bake_code=true` (alongside `use_published_images=true`) for init/check so deploy-stack flips api +
    onboard-gui to the baked /opt/kontroll. Guards a kit that pulls the images but tries to run /repo code that isn't
    there (the api/onboard-gui containers would crash on import)."""
    s = _read("scripts/kontroll.sh")
    assert "use_published_images=true -e bake_code=true" in s, \
        "the minimized-kit launcher must force BOTH published images AND baked code for init/check"


def test_promote_slice_is_baked_and_kept_in_the_canonical_m8a():
    """M8a (synthesis §2 Fork A MUST): the C10 promote slice + FIX-M9 generator must be (a) BAKED into the control
    image (so the services + a baked path read the digest-pinned copy) AND (b) KEPT in the kit/canonical (so the
    trusted `kontroll-promote` running from the per-job clone, and FIX-M9's proposed-tree extract, find it). Both come
    from the same repo `scripts/` tree, so they are byte-identical BY CONSTRUCTION — this check pins that neither side
    silently drops or forks the FF-gate / conflict-checker as the canonical shrinks. Guards a forked promote path."""
    dockerfile = _read("docker/semaphore-runner/Dockerfile")
    strip = _read("scripts/make-launch-kit.sh")
    assert "scripts/" in dockerfile and "/opt/kontroll/scripts/" in dockerfile, \
        "the runner image must bake scripts/ (so the promote slice + FIX-M9 generator are in the digest-pinned image)"
    for f in _PROMOTE_SLICE:
        # not individually stripped from the kit (scripts/ is kept whole — assert no targeted strip of these slips in)
        assert '"$dst"/%s' % f not in strip, "make-launch-kit must KEEP the promote-slice file %s in the canonical" % f


@pytest.mark.skipif(os.name == "nt" or not all(shutil.which(t) for t in ("bash", "git", "tar")),
                    reason="needs a POSIX bash + git + tar (on Windows `bash` resolves to the WSL stub) — runs in CI")
def test_launch_kit_assembles_without_instance_leak():
    """A REAL assembly (make-launch-kit.sh --allow-unpinned) must produce a tarball that contains the kontroll
    launcher + images.env + the public instance.example stub, and ZERO entries under the private instance/ overlay or
    docs/reviews. This is the image-world #72 blind-audit in miniature: a stranger who untars the kit learns nothing
    about the lab. Guards a strip regression a text test can't catch (e.g. a path typo that no longer matches)."""
    archive = os.path.join(ROOT, "dist", "kontroll-launch-pytest-c1.tar.gz")
    try:
        # PYTHON=sys.executable so the script renders images.env with the exact pyyaml-bearing interpreter (not a
        # PATH `python3` that may be a stub), keeping the live assembly deterministic across environments.
        subprocess.run(["bash", "scripts/make-launch-kit.sh", "pytest-c1", "--allow-unpinned"],
                       cwd=ROOT, check=True, capture_output=True, text=True,
                       env={**os.environ, "PYTHON": sys.executable})
        with tarfile.open(archive) as tf:
            names = tf.getnames()
        assert any(n.endswith("/kontroll") for n in names), "the kit must ship the kontroll launcher"
        assert any(n.endswith("/images.env") for n in names), "the kit must ship images.env"
        assert any("/instance.example/" in n for n in names), "the kit must ship the public instance.example stub"
        assert not any("/instance/" in n for n in names), "the kit must NOT leak the private instance/ overlay"
        assert not any("/docs/reviews/" in n for n in names), "the kit must NOT leak internal review snapshots"
        # Rung 3: the baked code is GONE (api/gui run from the published image), but the canonical-run closure stays.
        # Match the TOP-LEVEL kit dir (the component right under the kit root) — NOT a substring, else a kept nested
        # dir like `ansible/backends/api/` would false-trip the api/ check.
        top_level = {n.split("/")[1] for n in names if n.count("/") >= 1}
        for d in ("api", "gui", "tests"):
            assert d not in top_level, "the minimized kit must NOT ship the baked/dev dir %s/ (it runs baked)" % d
        for keep in ("scripts/kontroll-promote.py", "scripts/gen-requirements.py", "scripts/lib/plaintext-guard.sh",
                     "scripts/kontroll-canonical.py", "ansible/playbooks/files/canonical-update-hook.sh"):
            assert any(n.endswith("/" + keep) for n in names), \
                ("the kit must KEEP the canonical-run file %s (promote / FIX-M9 / the capture's plaintext gate / "
                 "the canonical's audit, which every deploy runs, and the hook it compares the canonical's with)"
                 % keep)
        for keep in ("ansible", "modules", "scripts"):
            assert keep in top_level, "the kit must KEEP the canonical-run dir %s/ (promote / deploy-stack)" % keep
    finally:
        if os.path.exists(archive):
            os.remove(archive)


def test_launcher_gates_the_capture_and_the_kit_ships_the_guard():
    """scripts/kontroll.sh runs scripts/lib/plaintext-guard.sh on the kit tree BEFORE its `git add -A`, exits when
    the guard returns non-zero, and does not test for the guard's existence first; make-launch-kit.sh does not
    strip scripts/lib. Guards the published-install path being the ungated one: on a launch-kit box this capture
    is the FIRST commit of a scaffolded instance/ (the deploy's own commit then finds a clean tree and never
    runs), and a kit that silently skipped a missing guard would capture everything."""
    s = _read("scripts/kontroll.sh")
    call = 'sh "$ROOT/scripts/lib/plaintext-guard.sh" "$ROOT" || {'
    assert call in s, "the launcher must run the plaintext guard and act on its status"
    assert s.index(call) < s.index('git -C "$ROOT" add -A'), "the guard must run before the blanket add"
    refusal = s[s.index(call):s.index('git -C "$ROOT" add -A')]
    assert "exit 1" in refusal, "a refusal must stop the launcher"
    assert "plaintext-guard.sh ]" not in s and "plaintext-guard.sh\" ]" not in s, \
        "no existence test: a kit without the guard must refuse, not skip"
    kit = _read("scripts/make-launch-kit.sh")
    assert '"$dst"/scripts/lib' not in kit and '"$dst"/scripts ' not in kit and '"$dst"/scripts\n' not in kit
    assert os.path.exists(os.path.join(ROOT, "scripts", "lib", "plaintext-guard.sh"))
