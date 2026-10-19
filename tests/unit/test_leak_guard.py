"""tests/_leak_guard.py — the identifier-leak guard behind every "no real topology / no personal identifier" gate.

WHY (the failures these guard, found by the 2026-10-08 public-split sweep):
- Capture-filename fixtures spelled a real lab address with DASHES (`cisco_ios_192-168-11-2.cfg`) and every earlier
  guard matched dotted IPs only, over a scope that did not include tests/. The structural layer must catch any
  separator form, over the whole shippable tree.
- The earlier guards EMBEDDED the very hostnames/domain they blocked (a public repo would publish its own denylist).
  The instance-token layer reads them from a private file instead; the canary file keeps the mechanism exercised
  on a checkout that has no private list, and the self-test here proves a scan that finds nothing is not a scan
  that ran nothing (a guard never observed to fail is not a guard).
- Python's `ipaddress.is_private` is true for the RFC-5737 documentation nets — the sanctioned example space — so a
  naive private-range check would flag every TEST-NET example; the RFC-1918 ranges are named explicitly.
- A failure report must never itself be the leak: a matched instance token is reported by index + digest only.
- (2026-10-09 review) The public cut ignored `instance/` outright, so an install made from the public tree committed
  NO overlay into the control node's canonical (`git add -A` skips ignored paths) and Semaphore silently read the
  example tier; the old "overlay never tracked" probe used `git check-ignore` without `--no-index`, which never
  reports a tracked path, so a `git add -f` passed it; and `docs/reviews/` was never scanned on any tree. The
  overlay invariant + the instance-only strip of docs/reviews/ fix all three; the scratch-repo tests below prove
  the shipped .gitignore rules with real git.
"""
import hashlib
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import _leak_guard as guard  # noqa: E402

pytestmark = pytest.mark.unit


def _cats(text):
    return sorted({cat for _, cat, _ in guard.structural_findings(text)})


def test_rfc1918_is_caught_in_dotted_dashed_and_underscored_forms():
    """A private address is flagged however it is spelled — dotted, dashed (capture filenames) or underscored — so
    the dashed-filename leak the sweep found can never recur in any file the public tree ships."""
    # (the samples are private-range SHAPES, deliberately not any real deployment's addresses; this file is the
    #  one file the tree scan skips — see _leak_guard.SELF_TEST — because its job is to hold such samples)
    assert _cats("ip: 192.168.77.252") == ["rfc1918-ipv4"]
    assert _cats('f = cap / "cisco_ios_192-168-77-2.cfg"') == ["rfc1918-ipv4"]
    assert _cats("FortiGate_192-168-88-5_config.cfg") == ["rfc1918-ipv4"]
    assert _cats("name_10_0_0_1") == ["rfc1918-ipv4"]
    assert _cats("172.16.4.9 and 10.9.8.7") == ["rfc1918-ipv4"]


def test_documentation_addresses_oids_and_versions_never_match():
    """The sanctioned example space (RFC-5737 TEST-NET, RFC-7042 MACs, RFC-3849 IPv6, loopback/link-local), a bare
    OID with address-shaped sub-identifiers and a version string are NOT findings — the gate must under-fire rather
    than cry wolf on the examples the docs are told to use (is_private would have flagged all of TEST-NET)."""
    assert _cats("addr 192.0.2.5 and 198.51.100.7 and 203.0.113.9") == []
    assert _cats("oid .1.3.6.1.2.1.10.0.0.1 = x") == []
    assert _cats("v10.0.0.1 release; ansible-core==2.19.4") == []
    assert _cats("mac 00:00:5e:00:53:01 null 00:00:00:00:00:00 bcast ff:ff:ff:ff:ff:ff") == []
    assert _cats("v6 2001:db8::1 fe80::1 ::1") == []
    assert _cats("t 12:30:45 on 2026:06:13") == []


def test_non_documentation_mac_and_global_ipv6_are_caught_in_every_spelling():
    """A real-looking MAC (colon, dash, Cisco dotted, net-snmp space-hex) and a global IPv6 outside the documentation
    prefix are findings — the channels an IPv4-only scan misses."""
    assert _cats("a4:5e:60:12:34:56") == ["mac"]
    assert _cats("a4-5e-60-12-34-56") == ["mac"]
    assert _cats("a45e.6012.3456") == ["mac"]                 # Cisco dotted form
    assert _cats("0011.2233.4455") == []                      # …but the textbook placeholder, dotted, is not
    assert _cats("Hex-STRING: A4 5E 60 12 34 56") == ["mac"]
    assert _cats("v6 2a02:1234:5678::1") == ["ipv6-global"]


def test_personal_identifier_and_key_material_shapes_are_caught():
    """An operator-machine user-profile path (drive-letter and MSYS forms), a real-length age recipient and a real-
    length AGE secret key are findings — the personal / key-material classes gitleaks' generic rules do not cover
    (the age placeholder in instance.example/.sops.yaml is deliberately one character short and never matches)."""
    assert _cats("C:" + chr(92) + "Users" + chr(92) + "someone" + chr(92) + "repo") == ["personal-identifier"]
    assert _cats("/c/Users/someone/repo") == ["personal-identifier"]
    assert _cats("age1" + "q" * 58) == ["age-recipient"]
    assert _cats("age1exampleexampleexampleexampleexampleexampleexampleexa") == []
    assert _cats("AGE-SECRET-KEY-1" + "Q" * 58) == ["age-secret-key"]
    assert _cats("AGE-SECRET-KEY-1FAKEZZ") == []


def test_allow_marker_exempts_exactly_its_line():
    """A same-line `pii-guard: allow <reason>` marker exempts that line only — the review-visible escape hatch for a
    test that MUST hold a private address (e.g. a private-unicast acceptance case). The next line is still scanned."""
    text = "ok 192.168.1.1  # pii-guard: allow private-unicast acceptance case\nbad 192.168.1.2\n"
    assert guard.structural_findings(text) == [(2, "rfc1918-ipv4", "192.168.1.2")]


def test_canary_self_test_the_instance_layer_fires_and_never_prints_the_token(tmp_path, monkeypatch):
    """The shipped canary list (the floor every checkout has, and the active layer on a public clone) is well-formed
    and a canary planted in a file IS flagged — so the token layer can never silently no-op. The report carries the
    token's index + an 8-hex sha256 of the match, never the matched text (a failure log must not itself be the
    leak). Also pins that SOME token source is active on this checkout (private list or canaries)."""
    assert guard.tokens_source(guard.ROOT) is not None, "no token source at all — the layer would be inert"
    canaries = os.path.join(guard.ROOT, "instance.example", "leak-tokens.example.txt")
    monkeypatch.setenv("KONTROLL_LEAK_TOKENS_FILE", canaries)
    pats = guard.instance_patterns(guard.ROOT)
    assert pats, "instance.example/leak-tokens.example.txt must ship at least one canary"
    # the canary is assembled at runtime so this tracked file never carries the token literally (on a public
    # checkout the canaries ARE the active token layer, and the guard scans this file too)
    canary = "kontroll-canary-" + "host"
    hits = guard.token_findings("hostname: %s\n" % canary, pats)
    assert hits and hits[0][1] == "instance-token"
    assert "canary" not in hits[0][2]
    assert hits[0][2].startswith("token#1 sha256:") and hits[0][2].endswith(
        hashlib.sha256(canary.encode()).hexdigest()[:8])


def test_private_token_file_takes_precedence_via_env(tmp_path, monkeypatch):
    """$KONTROLL_LEAK_TOKENS_FILE (how CI injects the private list as a secret) wins over the shipped canaries, is
    case-insensitive and word-bounded, and ignores comments/blank lines."""
    f = tmp_path / "tokens.txt"
    f.write_text("# comment\n\nmy-real-host\nlab\\.example\n", encoding="utf-8")
    monkeypatch.setenv("KONTROLL_LEAK_TOKENS_FILE", str(f))
    pats = guard.instance_patterns(guard.ROOT)
    assert len(pats) == 2
    assert guard.token_findings("host MY-REAL-HOST up; lab.example down", pats)
    assert guard.token_findings("my-real-hostname", pats) == []          # word-bounded: a longer name is not a hit


def test_shippable_tree_is_free_of_identifiers():
    """The standing gate over the WHOLE shippable tree (every tracked file minus instance/ and local/, and minus
    docs/reviews/ only on an instance tree — the make-bundle strip set): zero structural findings and zero
    instance-token findings. This is the pytest twin of `tests/validate.sh` step `pii-guard` and the public CI's
    PII-guard workflow; it runs with whatever token layer is present (the private list on the instance repo / CI
    secret, the canaries on a public checkout)."""
    files = guard.shippable_files(guard.ROOT)
    assert files, "git ls-files returned nothing — the scan would be vacuous"
    findings = guard.scan_paths(files, guard.ROOT)
    assert findings == [], "identifier leak(s) in the shippable tree:\n  " + "\n  ".join(
        "%s:%d %s %s" % f for f in findings[:40])


def test_this_tree_satisfies_the_overlay_invariant():
    """The tree under test is in one of the two valid overlay states — PUBLIC (nothing under instance/ tracked,
    the directory ignored) or INSTANCE (tracked, every tracked path re-included by instance/.gitignore). The pytest
    seat of the invariant the other two seats check through `--tree`; a `git add -f`, a lost root rule or a
    missing instance/.gitignore fails here with the offending paths."""
    assert guard.overlay_findings(guard.ROOT) == []


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd)] + list(args), capture_output=True, text=True, check=True).stdout


def _scratch_tree(where, with_reinclude, with_root_rule=True):
    """A throwaway git repo carrying the REAL root .gitignore (so the tests prove the shipped rules), a planted
    docs/reviews/ dossier quoting a private-range address, and an overlay with every entry class: allow-listed
    config, a key file inside an allow-listed dir, an ignored trust artifact, a stray top-level file."""
    repo = where / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    root_ignore = open(os.path.join(guard.ROOT, ".gitignore"), encoding="utf-8").read()
    assert "/instance/*\n" in root_ignore and "!/instance/.gitignore\n" in root_ignore, "the shipped root rule"
    if not with_root_rule:
        root_ignore = root_ignore.replace("/instance/*\n", "").replace("!/instance/.gitignore\n", "")
    (repo / ".gitignore").write_text(root_ignore, encoding="utf-8")
    (repo / "README.md").write_text("the tool\n", encoding="utf-8")
    dossier = repo / "docs" / "reviews" / "2026-10-09-x"
    dossier.mkdir(parents=True)
    dossier.joinpath("10-report.md").write_text("a dossier quoting 192.168.77.252 by mistake\n", encoding="utf-8")
    inst = repo / "instance"
    files = {
        ".sops.yaml": "creation_rules: []\n", "fleet.yml": "enabled_modules: []\n", "instance.yml": "x: 1\n",
        "leak-tokens.txt": "# none\n", "inventory/hosts.yml": "all: {}\n", "secrets/d.sops.yml": "a: ENC[x]\n",
        "secrets/keys.txt": "a key file, by name\n", "trust/keys/.gitkeep": "",
        "trust/observed-digests.yml": "x: 1\n", "control.agekey": "a key file, by suffix\n", "stray.txt": "stray\n",
        "actuation/demo/unit.yml": "key: demo\n", "actuation/demo/vars.yml": "x: 1\n",
        "certs/pve-ca.pem": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
        "certs/server.key": "a private key, by name\n", "OPERATIONS.md": "# Operations\n",
    }
    for rel, body in files.items():
        p = inst / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    if with_reinclude:
        shutil.copy(os.path.join(guard.ROOT, "instance.example", ".gitignore"), inst / ".gitignore")
    return repo


def _tracked_overlay(repo):
    return sorted(f for f in _git(repo, "ls-files").split("\n") if f.startswith("instance/"))


def test_public_tree_ignores_the_whole_overlay_and_scans_its_dossiers(tmp_path):
    """PUBLIC state (no instance/.gitignore): `git add -A` stages NOTHING under instance/ (the root `/instance/*`
    rule), the overlay invariant holds, and docs/reviews/ IS scanned — the planted private-range address in a
    dossier is a finding. Guards the strip set hiding a dossier leak on the public repository (M3) and the root
    rule going missing."""
    repo = _scratch_tree(tmp_path, with_reinclude=False)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "public")
    assert _tracked_overlay(repo) == []
    assert guard.overlay_findings(str(repo)) == []
    files = guard.shippable_files(str(repo))
    assert "docs/reviews/2026-10-09-x/10-report.md" in files, "a public tree scans its dossiers"
    findings = guard.scan_paths(files, str(repo), patterns=[])
    assert findings and {f[0] for f in findings} == {"docs/reviews/2026-10-09-x/10-report.md"}


def test_instance_tree_tracks_exactly_the_reincluded_overlay_entries(tmp_path):
    """INSTANCE state (instance/.gitignore scaffolded from instance.example/.gitignore): `git add -A` — the control
    node's canonical commit in local-canonical.yml — stages every known overlay entry (the app-store units under
    `actuation/`, a public certificate under `certs/` and the deployment's ledger `OPERATIONS.md` among them —
    without its re-include the ledger never reaches the canonical or a recovery clone) and NOTHING the root
    secret patterns cover:
    `keys.txt` and `*.agekey` inside the overlay, a `*.key` beside the certificate in the re-included `certs/`,
    `trust/observed-digests.yml` and a stray top-level file all stay untracked. The invariant holds and docs/reviews/ is stripped (an instance's dossiers quote its
    deployment). This is the install path the public cut had broken: a public install committed no overlay at all,
    so Semaphore read the example tier (M1)."""
    repo = _scratch_tree(tmp_path, with_reinclude=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "instance")
    assert _tracked_overlay(repo) == sorted([
        "instance/.gitignore", "instance/.sops.yaml", "instance/fleet.yml", "instance/instance.yml",
        "instance/leak-tokens.txt", "instance/inventory/hosts.yml", "instance/secrets/d.sops.yml",
        "instance/trust/keys/.gitkeep", "instance/actuation/demo/unit.yml", "instance/actuation/demo/vars.yml",
        "instance/certs/pve-ca.pem", "instance/OPERATIONS.md"])
    assert guard.overlay_findings(str(repo)) == []
    files = guard.shippable_files(str(repo))
    assert not any(f.startswith(("instance/", "docs/reviews/")) for f in files)
    assert guard.scan_paths(files, str(repo), patterns=[]) == []


def test_overlay_invariant_catches_a_force_add_a_lost_rule_and_a_dropped_reinclude(tmp_path):
    """The leak paths the old probe passed (M2): (a) `git add -f` of overlay files on a PUBLIC tree — tracked AND
    ignored — is a finding naming each path; (b) a root .gitignore without the `/instance/*` rule on a tree that
    tracks nothing under instance/ is a finding (the public repository's protection is gone); (c) an instance
    repository whose instance/.gitignore was deleted: every tracked overlay path becomes ignored and is reported
    (new overlay files would never reach the canonical)."""
    a = _scratch_tree(tmp_path / "a", with_reinclude=False)
    _git(a, "add", "-A")
    _git(a, "add", "-f", "instance/stray.txt", "instance/fleet.yml")
    _git(a, "commit", "-q", "-m", "force-added")
    bad = guard.overlay_findings(str(a))
    assert sorted(f[0] for f in bad) == ["instance/fleet.yml", "instance/stray.txt"]
    assert all(f[2] == "overlay" and "tracked AND git-ignored" in f[3] for f in bad)

    b = _scratch_tree(tmp_path / "b", with_reinclude=False, with_root_rule=False)
    _git(b, "add", ".gitignore", "README.md")
    _git(b, "commit", "-q", "-m", "no rule")
    lost = guard.overlay_findings(str(b))
    assert len(lost) == 1 and lost[0][0] == "instance/" and "lost its" in lost[0][3]

    c = _scratch_tree(tmp_path / "c", with_reinclude=True)
    _git(c, "add", "-A")
    _git(c, "commit", "-q", "-m", "instance")
    _git(c, "rm", "-q", "instance/.gitignore")
    _git(c, "commit", "-q", "-m", "dropped the re-include")
    dropped = guard.overlay_findings(str(c))
    assert sorted(f[0] for f in dropped) == [f for f in _tracked_overlay(c)], "every tracked overlay path is reported"


def test_tree_mode_folds_the_overlay_invariant_into_the_scan(tmp_path, capsys):
    """`--tree` — the entry point all three seats share — reports the overlay state in its summary line and turns an
    invariant violation into an `overlay` finding with exit 1, so validate, the workflow and this suite cannot
    disagree about it."""
    repo = _scratch_tree(tmp_path, with_reinclude=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "instance")
    assert guard.main(["--tree", str(repo)]) == 0
    assert "overlay = instance" in capsys.readouterr().out
    _git(repo, "rm", "-q", "instance/.gitignore")
    _git(repo, "commit", "-q", "-m", "dropped")
    assert guard.main(["--tree", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "  overlay  " in out and "instance/fleet.yml:0" in out
