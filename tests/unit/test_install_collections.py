"""The never-brick collection install wrapper (scripts/install-collections.py, R5 PR-B / C15-a, I-1).

WHY (the failures these guard):
  * with NO keyring on disk (the all-unsigned fleet default), the wrapper must issue a PLAIN `-r`
    install — byte-identical to the raw `ansible-galaxy collection install` it replaced, so the current
    fleet has ZERO behaviour change / zero brick risk (NB-1). The signature flags activate only once a
    keyring exists.
  * with a keyring present, it must add the EXACT never-brick mode (`--required-valid-signature-count
    all` + `--ignore-signature-status-codes NO_PUBKEY NODATA`) and NEVER a `+`-prefixed count (which
    fails on ABSENCE and would brick the unsigned fleet) — the source-traced brick-proof flag set.
  * the ignore-list must be EXACTLY {NO_PUBKEY, NODATA} — never a tamper code (BADSIG/ERRSIG/...), or a
    "fix a brick" edit could silence tamper (report 20 §7.4 invariant; NB-2).
  * an install failure (tamper: a signature that fails to verify) must PROPAGATE — the wrapper exits
    non-zero, so a tampered collection never installs silently (NB-2 fail-closed).
  * the provenance summary is loud but carries names/counts only — never a key or credential (NB-4).
"""
import hashlib
import importlib.util
import json
import os
import types

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_script(modname, filename):
    """Load a hyphenated scripts/<filename> by path (not importable as a module name)."""
    spec = importlib.util.spec_from_file_location(modname, os.path.join(ROOT, "scripts", filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wrap = _load_script("install_collections", "install-collections.py")
SIDECAR = {"keyring": "instance/trust/galaxy-pubkeys.gpg",
           "collections": [{"name": "cisco.ios", "signature_policy": "adaptive"},
                           {"name": "community.sops", "signature_policy": "adaptive"}]}


def test_no_keyring_is_a_plain_install():
    """No keyring on disk ⇒ a plain `-r` install (today's behaviour) — never the signature flags. The
    brick-proof default: an unsigned fleet with no keyring installs exactly as before (NB-1)."""
    argv = wrap.build_argv(SIDECAR, keyring_exists=False)
    assert argv == ["ansible-galaxy", "collection", "install", "-r", wrap.LOCKFILE]
    assert "--keyring" not in argv and "--required-valid-signature-count" not in argv


def test_keyring_present_adds_the_never_brick_mode():
    """A keyring on disk ⇒ the adaptive flags: --required-valid-signature-count all (NOT `+all`) +
    --ignore-signature-status-codes NO_PUBKEY NODATA + --keyring. verify-if-served / pass-if-absent."""
    argv = wrap.build_argv(SIDECAR, keyring_exists=True)
    assert "--keyring" in argv and argv[argv.index("--keyring") + 1] == SIDECAR["keyring"]
    assert argv[argv.index("--required-valid-signature-count") + 1] == "all"      # NEVER `+all` (bricks on absence)
    j = argv.index("--ignore-signature-status-codes")
    assert argv[j + 1:j + 3] == ["NO_PUBKEY", "NODATA"]


def test_never_emits_a_strict_plus_count():
    """No code path emits a `+`-prefixed signature count (fail-on-absence) — pinned across BOTH branches so a
    future edit can't sneak `+all` onto the fleet path and brick the unsigned fleet."""
    for keyring_exists in (True, False):
        assert not any(str(a).startswith("+") for a in wrap.build_argv(SIDECAR, keyring_exists))


def test_ignore_list_is_exactly_no_pubkey_nodata():
    """The ignore-list is EXACTLY {NO_PUBKEY, NODATA} and contains no tamper code — guards a widening that
    would silence BADSIG/ERRSIG/REVKEYSIG (report 20 §7.4; the NB-2 line absence-proceeds / tamper-fails)."""
    assert set(wrap.IGNORE_CODES) == {"NO_PUBKEY", "NODATA"}
    for tamper in ("BADSIG", "ERRSIG", "REVKEYSIG", "EXPKEYSIG", "FAILURE"):
        assert tamper not in wrap.IGNORE_CODES


def test_image_install_uses_the_venv_binary_and_install_path(monkeypatch):
    """In the runner-image build (I-2), the env overrides KONTROLL_GALAXY_BIN + KONTROLL_COLLECTIONS_PATH select
    the semaphore-venv ansible-galaxy + the `-p /usr/share/ansible/collections` target — so the SAME wrapper
    serves both seams (G-4 no-drift) without hardcoding the image layout. Still plain (no keyring) → never-brick."""
    monkeypatch.setenv("KONTROLL_GALAXY_BIN", "/opt/semaphore/v/venv/bin/ansible-galaxy")
    monkeypatch.setenv("KONTROLL_COLLECTIONS_PATH", "/usr/share/ansible/collections")
    argv = wrap.build_argv(SIDECAR, keyring_exists=False)
    assert argv[0] == "/opt/semaphore/v/venv/bin/ansible-galaxy"
    assert argv[argv.index("-p") + 1] == "/usr/share/ansible/collections"
    assert "--required-valid-signature-count" not in argv      # no keyring → plain install (the never-brick floor)


def test_bootstrap_seam_sets_no_env_overrides():
    """With NO env overrides (the I-1 bootstrap seam), the argv is the plain default — same binary on PATH, no
    `-p`. Guards that the I-2 generalization didn't change the bootstrap install (the byte-identical-to-today
    contract on the unsigned fleet)."""
    argv = wrap.build_argv(SIDECAR, keyring_exists=False)
    assert argv == ["ansible-galaxy", "collection", "install", "-r", wrap.LOCKFILE]


def test_dockerfile_installs_via_the_same_wrapper():
    """G-4 (the I-1 ≡ I-2 no-drift gate): the runner image build MUST install via scripts/install-collections.py
    — the SAME never-brick wrapper bootstrap.yml uses — not a raw `ansible-galaxy collection install`, and it MUST
    COPY the trust sidecar (BRICK-2) so the wrapper can read the per-collection policy. Guards a signature-posture
    drift between the two install seams (a `+`-flag / bare-all-without-ignore slipping onto one path only)."""
    with open(os.path.join(ROOT, "docker", "semaphore-runner", "Dockerfile"), encoding="utf-8") as fh:
        df = fh.read()
    assert "install-collections.py" in df, "the runner image must install via the never-brick wrapper"
    assert "ansible-galaxy collection install" not in df, (
        "the raw flat install must be gone from the Dockerfile — the wrapper owns the never-brick posture")
    assert "trust.generated.yml" in df, "the Dockerfile must COPY the trust sidecar (BRICK-2)"


def test_install_failure_propagates(monkeypatch):
    """On the PLAIN path (the runner image / no instance/trust), ansible-galaxy returning non-zero (a signature
    that fails to verify = tamper) makes the wrapper exit non-zero — fail-closed on tamper (NB-2), so a tampered
    collection never installs silently. l2b_root→None forces the plain branch under test."""
    monkeypatch.setattr(wrap, "_sidecar", lambda: {"collections": []})
    monkeypatch.setattr(wrap, "l2b_root", lambda: None)        # the image / no-trust-dir path
    monkeypatch.setattr(wrap.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=2))
    with pytest.raises(SystemExit) as ei:
        wrap.main()
    assert ei.value.code == 2


def test_summary_is_counts_only_no_secret():
    """The provenance summary reports collection COUNTS by policy (loud, NB-4) and carries no keyring path or
    credential — a green install must never imply a signature was verified on an unsigned fleet."""
    s = wrap.summary(SIDECAR)
    assert "2 collection(s)" in s and "adaptive=2" in s
    assert SIDECAR["keyring"] not in s and "pin + checksum" in s


# ── L2b — the recorded-sha256 floor (the substitute for the absent signature on a 0-sig fleet) ────────────────────
def test_version_satisfied_exact_vs_floor():
    """`==X` needs an EXACT match (a reinstall is owed otherwise). A `>=X` floor is a REAL constraint — met ONLY when
    installed >= X: a LOWER installed version is NOT satisfied (the fresh-Debian-12 shadow bug — a distro-preseeded
    community.docker 3.4.7 must not mask a `>=4.0.0` pin), at/above the floor IS. A bare spec is satisfied by ANY
    installed version (ansible-galaxy `-r` never upgrades it). Absent is never satisfied. One pure function."""
    assert wrap.version_satisfied("8.0.4", "==8.0.4")
    assert not wrap.version_satisfied("8.0.3", "==8.0.4")
    assert wrap.version_satisfied("4.0.0", ">=4.0.0")               # AT the floor → satisfied
    assert wrap.version_satisfied("5.2.1", ">=4.0.0")               # above the floor → satisfied
    assert not wrap.version_satisfied("3.4.7", ">=4.0.0")           # BELOW the floor → NOT satisfied (the shadow bug)
    assert wrap.version_satisfied("2.5.0", ">=2.0.0")
    assert wrap.version_satisfied("9.9.9", "5.0.0")                 # bare spec: present-at-any-version is enough
    assert not wrap.version_satisfied(None, "==8.0.4")
    assert not wrap.version_satisfied(None, ">=4.0.0")


def test_floor_shadow_is_to_install_not_skipped():
    """REGRESSION (the live fresh-Debian-12 break): a `>=4.0.0` pin with a LOWER version already reported installed
    (the apt `ansible` metapackage's community.docker 3.4.7 in dist-packages, which `collection list` surfaces) MUST
    be to-install — the too-old shadow does not meet the floor, so the wrapper reinstalls the pinned >=4.0 into the
    project path where the deploy looks (else docker_image_build, added in community.docker 3.6, goes unresolved). An
    ADEQUATE installed version is still skipped (idempotent — no needless reinstall)."""
    entries = [{"name": "community.docker", "version": ">=4.0.0"}]
    assert {e["name"] for e in wrap.to_install(entries, {"community.docker": "3.4.7"})} == {"community.docker"}
    assert wrap.to_install(entries, {"community.docker": "5.2.1"}) == []


def test_to_install_diffs_against_installed():
    """to_install keeps only the lockfile entries not already satisfied on disk — the only set the wrapper fetches.
    A present `==` match + a present floor are skipped; an absent name + an `==` version-mismatch are kept. Guards
    the air-gap-safe / idempotent re-run (a present pinned set ⇒ empty ⇒ no network)."""
    entries = [{"name": "a.b", "version": "==1.0.0"}, {"name": "c.d", "version": ">=2.0.0"},
               {"name": "e.f", "version": "==3.0.0"}, {"name": "g.h", "version": "==4.0.0"}]
    installed = {"a.b": "1.0.0", "c.d": "2.9.0", "e.f": "2.0.0"}   # e.f present but WRONG version; g.h absent
    assert {e["name"] for e in wrap.to_install(entries, installed)} == {"e.f", "g.h"}


def test_parse_artifact_filename():
    """`<namespace>-<name>-<version>.tar.gz` → `name==version` (the digest-lock key); a pre-release dash in the
    version is preserved; a non-artifact returns None. Version-layout-independent (namespace/name have no dashes)."""
    assert wrap.parse_artifact("community-docker-4.0.0.tar.gz") == "community.docker==4.0.0"
    assert wrap.parse_artifact("cisco-ios-8.0.0-rc1.tar.gz") == "cisco.ios==8.0.0-rc1"
    assert wrap.parse_artifact("not-an-artifact.txt") is None


def test_record_or_verify_tofu():
    """First sight of a key RECORDS it (TOFU, first-install); a re-sight of the SAME digest is 'ok'; a DIFFERENT
    digest is 'mismatch' (the caller fails closed). The whole L2b verdict in one pure function (MF-1)."""
    d = {}
    assert wrap.record_or_verify(d, "a.b==1.0.0", "deadbeef") == "recorded" and d["a.b==1.0.0"] == "deadbeef"
    assert wrap.record_or_verify(d, "a.b==1.0.0", "deadbeef") == "ok"
    assert wrap.record_or_verify(d, "a.b==1.0.0", "feedface") == "mismatch"


def test_digest_lock_roundtrip(tmp_path):
    """save_digests → load_digests is a faithful roundtrip (the per-node TOFU lock the next install re-verifies
    against); the header marks it generated/do-not-edit. Guards the lock format the fail-closed gate keys off."""
    lock = tmp_path / "observed-digests.yml"
    wrap.save_digests({"a.b==1.0.0": "abc", "c.d==2.0.0": "def"}, path=str(lock))
    assert wrap.load_digests(str(lock)) == {"a.b==1.0.0": "abc", "c.d==2.0.0": "def"}
    assert "DO NOT EDIT" in lock.read_text(encoding="utf-8")


class _FakeGalaxy:
    """A subprocess.run stand-in for the 3 ansible-galaxy verbs L2b drives: `collection list` (returns installed
    JSON), `collection download` (drops the given tarball bytes + a requirements.yml into the `-p` dir), `collection
    install` (records the argv + returns a rc). Records every argv for assertions."""
    def __init__(self, installed=None, tarballs=None, install_rc=0, list_rc=0):
        self.installed = installed or {}
        self.tarballs = tarballs or {}                            # {filename: bytes} dropped on `download`
        self.install_rc = install_rc
        self.list_rc = list_rc
        self.calls = []

    def __call__(self, argv, cwd=None, capture_output=False, text=False):
        self.calls.append(list(argv))
        verb = argv[2] if len(argv) > 2 else ""
        if verb == "list":
            payload = json.dumps({"/p": {n: {"version": v} for n, v in self.installed.items()}})
            return types.SimpleNamespace(returncode=self.list_rc, stdout=payload, stderr="")
        if verb == "download":
            dl = argv[argv.index("-p") + 1]
            for fn, content in self.tarballs.items():
                with open(os.path.join(dl, fn), "wb") as fh:
                    fh.write(content)
            with open(os.path.join(dl, "requirements.yml"), "w", encoding="utf-8") as fh:
                fh.write("collections: []\n")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=self.install_rc)  # install (not captured in the wrapper)

    def verbs(self):
        return [c[2] for c in self.calls if len(c) > 2]


@pytest.fixture
def l2b_env(tmp_path, monkeypatch):
    """L2b engaged against a tmp trust root + lock; the lockfile is one `==` pin (community.docker==4.0.0).
    Returns the lock path so a test can pre-seed or read it."""
    trust = tmp_path / "instance" / "trust"
    trust.mkdir(parents=True)
    monkeypatch.setattr(wrap, "TRUST_DIR", str(trust))
    monkeypatch.setattr(wrap, "DIGEST_LOCK", str(trust / "observed-digests.yml"))
    monkeypatch.setattr(wrap, "_sidecar", lambda: {"collections": [{"name": "community.docker"}]})
    monkeypatch.setattr(wrap, "lockfile_entries",
                        lambda path=None: [{"name": "community.docker", "version": "==4.0.0"}])
    return trust / "observed-digests.yml"


def test_main_plain_path_when_no_trust_dir(monkeypatch):
    """No instance/trust (the runner image, I-2) ⇒ main() runs the PLAIN `-r` install — no list/download/L2b. The
    byte-identical-to-today image path; L2b is control-node-only (synthesis §5: the L2b shape is wired at I-1)."""
    monkeypatch.setattr(wrap, "_sidecar", lambda: {"collections": []})
    monkeypatch.setattr(wrap, "l2b_root", lambda: None)
    fake = _FakeGalaxy()
    with pytest.raises(SystemExit) as ei:
        wrap.main(run=fake)
    assert ei.value.code == 0 and fake.verbs() == ["install"]     # straight to the plain install (no fetch diff)


def test_main_idempotent_when_present_does_no_fetch(l2b_env):
    """L2b engaged + the pinned set already installed ⇒ main() exits 0 having run ONLY `collection list` (no
    download, no install) — air-gap-safe + idempotent (no 'Installing' ⇒ 0 changed). Guards the brick the design
    must not reintroduce: a re-run on a transiently-offline node that needs nothing must not try to fetch."""
    fake = _FakeGalaxy(installed={"community.docker": "4.0.0"})
    with pytest.raises(SystemExit) as ei:
        wrap.main(run=fake)
    assert ei.value.code == 0 and fake.verbs() == ["list"]        # no download, no install


def test_main_l2b_records_then_installs_offline(l2b_env):
    """A fresh install: list (absent) → download → record the tarball's sha256 (TOFU) → install the verified tarball
    BY PATH with --offline. Asserts the install carries --offline + the tarball path itself (MF-2: the bytes verified
    ARE the bytes installed — by absolute path, not the download dir's relative-source requirements) and the digest
    was persisted to the lock. The install-by-path shape was corrected from `-r reqs` by the live dogfood (the
    download's requirements.yml `source` is relative to the download dir, unreachable from cwd=ANSIBLE_DIR)."""
    fake = _FakeGalaxy(installed={}, tarballs={"community-docker-4.0.0.tar.gz": b"REALBYTES"})
    with pytest.raises(SystemExit) as ei:
        wrap.main(run=fake)
    assert ei.value.code == 0 and fake.verbs() == ["list", "download", "install"]
    install = next(c for c in fake.calls if c[2] == "install")
    assert "--offline" in install and any(a.endswith("community-docker-4.0.0.tar.gz") for a in install)
    assert "-r" not in install                                       # installed BY PATH, not via the download reqs file
    assert wrap.load_digests(str(l2b_env))["community.docker==4.0.0"] == hashlib.sha256(b"REALBYTES").hexdigest()


def test_main_l2b_prints_recorded_on_a_first_record_only(l2b_env, capsys):
    """A digest recorded for the first time prints `  L2b recorded  <name==version>`; the same bytes again, the
    collection gone as it is when the installer container ends, print `  L2b ok …` and no `recorded`. bootstrap's
    install task reads `L2b recorded` as the change a container run keeps (I-3: the install ends with the
    container, the record in instance/trust/ stays). Guards the word moving under that signal, which would
    report a first trust record as no change again."""
    tarballs = {"community-docker-4.0.0.tar.gz": b"REALBYTES"}
    with pytest.raises(SystemExit) as ei:
        wrap.main(run=_FakeGalaxy(installed={}, tarballs=tarballs))
    first = capsys.readouterr().out.splitlines()
    assert ei.value.code == 0 and "  L2b recorded  community.docker==4.0.0" in first, first
    with pytest.raises(SystemExit) as ei:
        wrap.main(run=_FakeGalaxy(installed={}, tarballs=tarballs))
    second = capsys.readouterr().out.splitlines()
    assert ei.value.code == 0 and "  L2b ok        community.docker==4.0.0" in second, second
    assert not [line for line in second if "L2b recorded" in line], second


def test_main_l2b_fail_closed_on_digest_mismatch(l2b_env):
    """A recorded pin whose served tarball now hashes DIFFERENTLY ⇒ main() exits non-zero and NEVER calls install —
    L2b fail-closed on a possible whole-artifact swap of a recorded version (the central security claim, MF-1).
    Pre-seeds the lock with a different digest for the same name==version, then serves swapped bytes."""
    wrap.save_digests({"community.docker==4.0.0": "0" * 64}, path=str(l2b_env))   # a WRONG recorded digest
    fake = _FakeGalaxy(installed={}, tarballs={"community-docker-4.0.0.tar.gz": b"SWAPPED"})
    with pytest.raises(SystemExit) as ei:
        wrap.main(run=fake)
    assert ei.value.code == 3 and "install" not in fake.verbs()   # the swap NEVER installs
