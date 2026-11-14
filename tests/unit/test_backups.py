"""backups — the read-only per-device backup INDEX / VIEW / DIFF viewer (#131, gap 1 / C14).

These pin service/backups.py: a PURE read of the secret-bearing captures git store served over the GUI. WHY this is
the riskiest surface in the GUI — it puts crown-jewel device configs (hashes, PSKs, SNMP communities) on an HTTP
wire — so the tests are the GATE's machine-checks, not nice-to-haves:

  * G1 (M1): the captures mount is `:ro`, asserted on the literal `:ro` SUFFIX (a bare source:target defaults to
    `:rw`; a "`:rw` absent" check would fail-OPEN silently).
  * G2 (M2): a bad rev/file is rejected BEFORE any `git show` (path-traversal + rev-injection closed; the
    allow-list-membership file guard is stronger than a regex).
  * G3 (M3+M4): the redaction registry masks the REAL lab vendor token SHAPES (Cisco/FortiGate/OPNsense/RouterOS),
    validated with real-shaped lines (synthetic secret payloads — never a real lab hash); redact-then-diff never
    leaks that *a* secret changed; redaction is defense-in-depth, NOT the boundary.
  * G4 (M5): no capture value reaches the audit log (asserted in tests/integration/test_gui_api.py).
  * read-only BY CONSTRUCTION: the AST pin + the argv git-verb pin forbid any write/actuation path.

A missing store degrades to {available:false} and NEVER raises / NEVER sys.exits the in-process worker.
"""
import os
import re
import shutil
import subprocess

import pytest
import yaml

from _readonly_pins import assert_no_git_write, assert_read_only
from kontroll.service import backups

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("isolated_git")]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HAVE_GIT = shutil.which("git") is not None
requires_git = pytest.mark.skipif(not _HAVE_GIT, reason="git binary not available")

# The PEM private-key opener marker, built from SPLIT literals so the contiguous `-----BEGIN … PRIVATE KEY-----`
# string never appears in committed source (it would trip gitleaks' bodyless-private-key rule + is poor hygiene).
# The runtime value is the real marker the redaction registry's pem_key_block_opener row must mask.
_PEM_BEGIN = "-----BEGIN ENCRYPTED " + "PRIVATE KEY-----"

# Real lab token SHAPES (from the migration repo's per-vendor baselines) carrying SYNTHETIC secret payloads — the
# regression must prove the registry masks the real LINE FORMAT; it must NOT commit a real lab hash (a secret). The
# payload is the second tuple element (what must NOT survive redaction).
_REAL_SHAPED_SECRETS = {
    "cisco_type9":    ("username admin privilege 15 secret 9 $9$SYNTHfakeHASHnotRealZZ", "$9$SYNTHfakeHASHnotRealZZ"),
    "cisco_type7_pw": ("username monitor password 7 06150E2F4A5C0SYNTH", "06150E2F4A5C0SYNTH"),
    "cisco_snmp":     ("snmp-server community S3cr3tC0mmFAKE RO", "S3cr3tC0mmFAKE"),
    "cisco_keystr":   ("  key-string 7 13061E010803SYNTH", "13061E010803SYNTH"),
    "forti_password": ("        set password ENC FAKEb64BLOBnotRealAAAABBBB==", "FAKEb64BLOBnotRealAAAABBBB=="),
    "forti_icl_key":  ("    set inter-controller-key ENC ZZZZfakeBLOBnotreal99==", "ZZZZfakeBLOBnotreal99=="),
    "opnsense_pw":    ("      <password>$2y$11$FAKEbcryptSyntheticNotReal0000</password>", "$2y$11$FAKEbcryptSyntheticNotReal0000"),
    "routeros_radius": ("set accounting=yes radius-password=FAKEr0sSecret store-leases=5m", "FAKEr0sSecret"),
    "routeros_wpa":   ('set security-profiles wpa2-pre-shared-key="FAKE psk not real"', "FAKE psk not real"),
    # PEM private-key OPENER (FortiGate `set private-key "-----BEGIN …`). Line-oriented redaction masks the opener
    # marker only (the multi-line base64 body is structurally unmaskable — named in SECURITY.md C14); the vendor that
    # emits it (FortiGate) is history-excluded → never rendered, so this is latent defense-in-depth.
    "pem_key_opener": ('        set private-key "' + _PEM_BEGIN, _PEM_BEGIN),
}
# Lines that are NOT secrets — they must pass through legibly (over-redaction corrupts the operator's view). Each
# echoes a real benign config line that a too-greedy regex would mangle.
_NON_SECRETS = [
    "interface Vlan100",
    "set admin-ssh-password enable",            # FortiOS: the word "password" but no ENC / no type-digit
    " description MGMT uplink to core",
    "set engine-id-suffix=\"\"",                # RouterOS: a key=value that is not a secret key
]


def _run(cwd, *args):
    subprocess.run(["git", "-C", str(cwd)] + list(args), check=True, capture_output=True, text=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"})


@pytest.fixture
def tmp_captures(tmp_path, monkeypatch):
    """A throwaway git captures store for the read-only viewer (C14): a real git repo with TWO revisions of a
    tracked Cisco capture (so log/show/diff have history) — where BOTH a non-secret line AND the secret value change
    between revs — plus an EXCLUDED on-disk-only FortiGate file (no commit) to exercise the diffable=false path.
    Points KONTROLL_CAPTURES_DIR at it so service/backups.py reads the throwaway, never a real store. paths.ROOT is
    left at the REAL repo so redact() loads the real config/capture-redactions.yml registry (the point of the
    redaction tests)."""
    cap = tmp_path / "captures"
    cap.mkdir()
    _run(cap, "init", "-q", "-b", "main")
    # the real backup-configs.yml writes + commits a .gitignore (capture-exception excludes); mirror it so the
    # fixture exercises the dot/meta-file filtering (a committed .gitignore must NOT show as a bogus device row).
    (cap / ".gitignore").write_text("# GENERATED from capture-exceptions.yml\nFortiGate_*\n", encoding="utf-8")
    f = cap / "cisco_ios_192-0-2-2.cfg"
    f.write_text("interface Vlan100\n description MGMT-OLD\n"
                 "username admin secret 9 $9$OLDsynthHASHnotrealAA\n!\n", encoding="utf-8")
    _run(cap, "add", "-A"); _run(cap, "commit", "-q", "-m", "backup 2026-06-17T02:00:02Z")
    f.write_text("interface Vlan100\n description MGMT-NEW\n ip address 192.0.2.2 255.255.255.0\n"
                 "username admin secret 9 $9$NEWsynthHASHnotrealBB\n!\n", encoding="utf-8")
    _run(cap, "add", "-A"); _run(cap, "commit", "-q", "-m", "backup 2026-06-18T02:00:03Z")
    (cap / "FortiGate_192-0-2-5_config.cfg").write_text(           # on-disk only, NEVER committed (excluded)
        "config system\n  set password ENC FAKEblobNOTreal==\nend\n", encoding="utf-8")
    monkeypatch.setenv("KONTROLL_CAPTURES_DIR", str(cap))
    return cap


# ---------------------------------------------------------------------------------------- index / version list --

@requires_git
def test_list_devices_lists_tracked_and_excluded(tmp_captures):
    """The tracked `.cfg` shows diffable=true + rev_count 2 + a latest rev; the on-disk-only FortiGate shows
    diffable=false + rev_count 0. Guards the "excluded device reads as latest-only, never 'no config'" model — and
    that the index surfaces an excluded capture WITHOUT pretending it has history."""
    out = backups.list_devices()
    assert out["available"] is True
    by_file = {d["file"]: d for d in out["devices"]}
    cisco = by_file["cisco_ios_192-0-2-2.cfg"]
    assert cisco["diffable"] is True and cisco["rev_count"] == 2 and cisco["latest"]["rev"]
    assert cisco["host_key"] == "192-0-2-2"
    forti = by_file["FortiGate_192-0-2-5_config.cfg"]
    assert forti["diffable"] is False and forti["rev_count"] == 0 and forti["latest"] is None


@requires_git
def test_list_devices_excludes_the_gitignore_meta_file(tmp_captures):
    """The store's own committed `.gitignore` (backup-configs.yml writes it to carry the capture-exception excludes)
    must NOT surface as a 'device' row, and must not be viewable. WHY: `git ls-files` lists it like any tracked file,
    so before the `_is_capture_file` filter it showed in the index as a bogus device (label '.gitignore', host_key '')
    — found live during the 2026-06-24 C14 prod dogfood. Guards BOTH the index (no dot/meta row) and the allow-list
    (a dot/meta file is rejected by `_validate_file`, so it stays unviewable/undiffable — symmetric with the index)."""
    files = {d["file"] for d in backups.list_devices()["devices"]}
    assert ".gitignore" not in files                              # the bug: no bogus meta row in the index
    assert "cisco_ios_192-0-2-2.cfg" in files                  # real captures are still listed
    assert all(not f.startswith(".") for f in files), files       # no dotfile leaks at all
    with pytest.raises(backups.BackupStoreError):                 # the allow-list rejects it too (not merely hidden)
        backups._validate_file(str(tmp_captures), ".gitignore")


@requires_git
def test_for_host_filters_to_the_dashed_host(tmp_captures):
    """for_host(dotted ansible_host) returns only the capture file(s) whose name carries the dashed host — the
    per-host drill-in glue stays SERVER-side (the file is the key, not a client guess). Guards a drill-in that
    silently widens to the whole store."""
    out = backups.for_host("192.0.2.2")
    assert {d["file"] for d in out["devices"]} == {"cisco_ios_192-0-2-2.cfg"}
    assert backups.for_host("203.0.113.99")["devices"] == []      # a host with no captures → empty, not all


@requires_git
def test_list_versions_newest_first(tmp_captures):
    """The version list returns both revisions newest-first with {rev,short,iso,subject} — the picker payload the
    two <select>s render. A wrong order/parse would mislabel which rev is newer (a diff-direction bug)."""
    out = backups.list_versions("cisco_ios_192-0-2-2.cfg")
    assert out["diffable"] is True and len(out["revisions"]) == 2
    assert out["revisions"][0]["subject"].endswith("02:00:03Z")   # newest first
    assert all(re.match(r"^[0-9a-f]{40}$", r["rev"]) and r["short"] == r["rev"][:7] for r in out["revisions"])


# ----------------------------------------------------------------------------------------- redaction (C14/G3) --

@requires_git
def test_get_version_redacts_secret_before_the_wire(tmp_captures):
    """get_version masks the type-9 secret hash (`secret 9 ‹redacted›`) and sets redacted=true; the raw hash must
    NOT appear in the returned lines. The load-bearing C14 assertion: a capture's secret is redacted BEFORE the
    bytes leave the process."""
    out = backups.get_version("cisco_ios_192-0-2-2.cfg", backups.list_versions("cisco_ios_192-0-2-2.cfg")["revisions"][0]["rev"])
    body = "\n".join(out["lines"])
    assert out["redacted"] is True
    assert "$9$NEWsynthHASHnotrealBB" not in body and "‹redacted›" in body
    assert "interface Vlan100" in body                        # non-secret structure preserved (legible view)


def test_redaction_registry_masks_real_vendor_shapes():
    """The shipped config/capture-redactions.yml masks the REAL lab vendor token SHAPES (Cisco type-7/9 + SNMP +
    key-string, FortiGate `set <k> ENC`, OPNsense `<password>`, RouterOS `radius-password=`/`wpa2-pre-shared-key=`)
    — the regression that a registry edit can't silently un-mask a known shape (M4). Uses real-shaped lines with
    SYNTHETIC payloads (never a real lab hash). Every payload must be gone; the mask must be present."""
    for name, (line, secret) in _REAL_SHAPED_SECRETS.items():
        red, hit = backups.redact(line)
        assert hit is True, "%s: registry did not match a real-shaped secret line" % name
        assert secret not in red, "%s: secret payload leaked through redaction: %r" % (name, red)
        assert "‹redacted›" in red, "%s: mask not applied" % name


def test_redaction_does_not_over_redact_benign_lines():
    """Non-secret lines pass through UNCHANGED (over-redaction corrupts the operator's view as surely as
    under-redaction leaks). Guards the type-digit/ENC anchoring that keeps `set admin-ssh-password enable` and a
    plain `interface`/`description` legible (M4's over-redaction half)."""
    for line in _NON_SECRETS:
        red, hit = backups.redact(line)
        assert red == line and hit is False, "benign line was mangled: %r -> %r" % (line, red)


def test_redaction_registry_patterns_have_exactly_one_group():
    """Every shipped redaction pattern compiles and has EXACTLY ONE capturing group (the secret span the masker
    replaces). A zero/multi-group pattern would mask nothing or the wrong span — the loader skips it, but the
    registry itself must be correct, so this fails loudly at the source."""
    doc = yaml.safe_load(open(os.path.join(ROOT, "config", "capture-redactions.yml"), encoding="utf-8").read())
    assert doc.get("redactions"), "the registry must ship at least the seed vendor patterns"
    for entry in doc["redactions"]:
        pat = re.compile(entry["pattern"])               # must compile
        assert pat.groups == 1, "%s: pattern must have exactly one capturing group" % entry.get("subject")


# ----------------------------------------------------------------------------------------------- diff (C14/G3) --

@requires_git
def test_diff_redact_then_diff_never_leaks_a_secret(tmp_captures):
    """The diff between the two revs (where the description AND the secret value both changed) renders the
    description change but NEVER any secret hash — redact-then-diff masks both sides of the secret line so it
    collapses to masked-vs-masked (or masked context), never leaking that *a* secret changed (C11). The
    load-bearing redact-then-diff assertion."""
    revs = backups.list_versions("cisco_ios_192-0-2-2.cfg")["revisions"]
    out = backups.diff_versions("cisco_ios_192-0-2-2.cfg", revs[1]["rev"], revs[0]["rev"])
    all_text = "\n".join(ln["text"] for h in out["hunks"] for ln in h["lines"])
    assert "$9$OLDsynthHASHnotrealAA" not in all_text and "$9$NEWsynthHASHnotrealBB" not in all_text
    assert "MGMT-NEW" in all_text and "MGMT-OLD" in all_text   # the non-secret change IS surfaced
    assert out["redacted"] is True


@requires_git
def test_diff_structured_hunks_shape(tmp_captures):
    """The diff yields hunks of {t ∈ ctx|add|del, ol, nl, text} lines + {added,removed} stats — the contract the
    dumb client renderer consumes (a wrong shape would break the painter)."""
    revs = backups.list_versions("cisco_ios_192-0-2-2.cfg")["revisions"]
    out = backups.diff_versions("cisco_ios_192-0-2-2.cfg", revs[1]["rev"], revs[0]["rev"])
    assert out["stats"]["added"] >= 1 and out["stats"]["removed"] >= 1
    line = out["hunks"][0]["lines"][0]
    assert set(line) == {"t", "ol", "nl", "text"} and line["t"] in ("ctx", "add", "del")


def test_difflib_hunks_line_number_arithmetic():
    """_difflib_hunks reconstructs ol/nl from the @@ header arithmetic: a ctx line carries both counters, an add
    carries nl (ol=None), a del carries ol (nl=None), and they advance correctly. Guards the one 'parse a diff'
    routine the client trusts for line numbers (review §7.6)."""
    a = ["a", "b", "c", "d"]
    b = ["a", "B", "c", "d", "e"]
    hunks, stats = backups._difflib_hunks(a, b)
    flat = [ln for h in hunks for ln in h["lines"]]
    added = [ln for ln in flat if ln["t"] == "add"]
    deleted = [ln for ln in flat if ln["t"] == "del"]
    assert stats == {"added": 2, "removed": 1}
    assert any(ln["text"] == "B" and ln["ol"] is None and ln["nl"] == 2 for ln in added)
    assert any(ln["text"] == "b" and ln["nl"] is None and ln["ol"] == 2 for ln in deleted)
    assert any(ln["text"] == "e" and ln["nl"] == 5 for ln in added)


# ---------------------------------------------------------------------------- input validation / traversal (G2) --

@requires_git
def test_rev_injection_rejected_before_git_show(tmp_captures, monkeypatch):
    """A non-hex rev (`HEAD; rm -rf /`, `../x`, `--upload-pack=x`) raises BackupStoreError and NEVER reaches a
    `git show` — rev injection closed (G2). Mocks _git to record every call and asserts no `show` ran."""
    calls = []
    real_git = backups._git
    monkeypatch.setattr(backups, "_git", lambda cap, args, **k: calls.append(args[0]) or real_git(cap, args, **k))
    for bad in ("HEAD; rm -rf /", "../x", "--upload-pack=x", "main", "abc123def456abc123def456abc123def456abcdef0"):
        with pytest.raises(backups.BackupStoreError):
            backups.get_version("cisco_ios_192-0-2-2.cfg", bad)
    assert "show" not in calls                                # a bad rev never reached the blob read


@requires_git
def test_file_traversal_rejected_before_git_show(tmp_captures, monkeypatch):
    """A file NOT in the store's allow-list (`../../instance/secrets/network.sops.yml`, an absolute path) raises
    and NEVER reaches `git show` — path traversal closed by allow-list membership (G2/M2). The allow-list
    (`ls-files`) may run, but `show` must not."""
    calls = []
    real_git = backups._git
    monkeypatch.setattr(backups, "_git", lambda cap, args, **k: calls.append(args[0]) or real_git(cap, args, **k))
    rev = backups.list_versions("cisco_ios_192-0-2-2.cfg")["revisions"][0]["rev"]
    for bad in ("../../instance/secrets/network.sops.yml", "/etc/passwd", "cisco_ios_192-0-2-2.cfg\x00.x"):
        with pytest.raises(backups.BackupStoreError):
            backups.get_version(bad, rev)
    assert "show" not in calls


@requires_git
def test_output_is_bounded(tmp_captures, monkeypatch):
    """A capture blob exceeding _MAX_BYTES raises rather than streaming MB into the worker — the DoS ceiling. Guards
    a giant capture from blowing the payload/memory of the in-process Flask worker."""
    monkeypatch.setattr(backups, "_MAX_BYTES", 16)            # tiny ceiling so the fixture's small blob trips it
    rev = backups.list_versions("cisco_ios_192-0-2-2.cfg")["revisions"][0]["rev"]
    with pytest.raises(backups.BackupStoreError):
        backups.get_version("cisco_ios_192-0-2-2.cfg", rev)


# ---------------------------------------------------------------------------------- graceful-absent (never crash) --

def test_absent_store_is_graceful_never_raises(monkeypatch):
    """With KONTROLL_CAPTURES_DIR unset / pointed at a non-git dir, every read returns {available:false} + a reason
    — NEVER an exception, NEVER sys.exit (the worker-survival INVARIANT). A fresh node without the captures mount
    shows an honest 'not mounted' state, not a 500."""
    monkeypatch.delenv("KONTROLL_CAPTURES_DIR", raising=False)
    for call in (lambda: backups.list_devices(),
                 lambda: backups.list_versions("x.cfg"),
                 lambda: backups.get_version("x.cfg", "abc1234"),
                 lambda: backups.diff_versions("x.cfg", "abc1234", "def5678")):
        out = call()
        assert out["available"] is False and out["reason"]


def test_unreadable_registry_fails_toward_caution(monkeypatch):
    """If the redaction registry is unreadable, redact() returns the text with redacted=True (the UI keeps the
    'secrets masked (best-effort)' caveat) rather than silently claiming a clean read — fail toward caution. Guards
    a missing/corrupt registry from quietly dropping the redaction signal."""
    monkeypatch.setattr(backups, "_RED_CACHE", {})
    monkeypatch.setattr(backups, "_redactions_path", lambda: os.path.join(ROOT, "does-not-exist.yml"))
    red, hit = backups.redact("username admin secret 9 $9$whatever")
    assert hit is True                                        # flag stays truthy on an unreadable registry


# ---------------------------------------------------------------------------- read-only-by-construction (the pin) --

def test_service_is_read_only_by_construction():
    """The viewer must NEVER gain a write/actuation path: assert_read_only pins each public read calls no write
    verb, and assert_no_git_write pins that every `_git(...)` call site picks a READ-ONLY git subcommand from a
    static list (a `git rm`/`commit` riding a subprocess arg list would slip past a call-name check). The
    machine-checked C14 read-only guarantee — a future edit adding a mutating git verb fails CI."""
    for fn in ("store_state", "list_devices", "for_host", "list_versions", "get_version", "diff_versions", "redact"):
        assert_read_only("scripts/kontroll/service/backups.py", fn)
    assert_no_git_write("scripts/kontroll/service/backups.py")


# ---------------------------------------------------------------------------------- the compose :ro mount (G1/M1) --

def test_onboard_gui_mounts_captures_read_only():
    """G1 (M1): onboard-gui.yaml mounts the captures store `:ro` — asserted on the literal `:ro` SUFFIX, NOT on
    '`:rw` absent' (a bare source:target defaults to :rw, so a missing-mode line would pass a naive negative check
    while mounting read-WRITE → a GUI RCE could rewrite the secret history). The single place a sketch build could
    fail-OPEN silently."""
    doc = yaml.safe_load(open(os.path.join(ROOT, "docker", "services", "onboard-gui.yaml"), encoding="utf-8").read())
    vols = doc["services"]["onboard-gui"]["volumes"]
    # find the captures mount by its /backups TARGET (the source carries `${VAR:-default}` colons, so match the
    # target, not a naive split), then assert the literal `:ro` SUFFIX is present — never merely '`:rw` absent'.
    target = [v for v in vols if isinstance(v, str) and re.search(r":/backups(:|$)", v)]
    assert len(target) == 1, "exactly one captures mount expected, found: %r" % target
    assert target[0].rstrip().endswith(":/backups:ro"), "captures mount MUST end with ':ro', got: %r" % target[0]


# --------------------------------------------------------------------------------------- the GUI template wiring --

def test_template_wires_the_backup_panel():
    """The header exposes a `backup-open` button → openBackupPanel() that fetches `/api/backups` and renders a
    `backup-device` index with `backup-base`/`backup-compare` pickers, a `backup-view`/`diff-view`, and the
    `backup-redacted-note` caveat; the per-host drill-in is `fleet-backup-open`. Pins the GUI round-trip + that the
    'secrets masked' honesty caveat and the read-only framing are present at the template level (testids recorded
    in testid_reference.md)."""
    tpl = open(os.path.join(ROOT, "gui", "templates", "index.html"), encoding="utf-8").read()
    for needle in ('data-testid="backup-open"', "openBackupPanel(", "/api/backups", "'backup-device'", "'backup-base'",
                   "'backup-compare'", "'backup-view'", "'diff-view'", "'backup-redacted-note'", "'fleet-backup-open'"):
        assert needle in tpl, "missing template wiring: %s" % needle
    assert "textContent" in tpl and "secrets masked" in tpl    # the no-XSS paint + the honesty caveat


def test_backup_headers_paint_device_bytes_via_textcontent_not_innerhtml():
    """The device-controlled capture filename/label (and the ansible_host caption) in the backup-panel HEADERS must
    be painted via the ET() textContent helper, NOT E()'s innerHTML — else a `<…>`-bearing capture filename or
    inventory ansible_host would EXECUTE in the operator's authenticated session (the one XSS sink the riskiest GUI
    surface must not have). Guards a regression of the 'every device byte via textContent' invariant the adversarial
    review (CORR-1/XSS-1) caught: the line BODIES were safe, the three headers were not."""
    tpl = open(os.path.join(ROOT, "gui", "templates", "index.html"), encoding="utf-8").read()
    assert "const ET = " in tpl                                # the textContent helper exists
    assert "ET('h3', null, d.label || d.file)" in tpl          # device-section header → textContent
    assert "ET('div', 'meta', file + ' · '" in tpl             # view head (file) → textContent
    assert "ET('div', 'meta', diff.file + ' · '" in tpl        # diff head (diff.file) → textContent
    assert "createTextNode(' · ' + host)" in tpl               # overlay caption host → textContent
    # the OLD innerHTML concatenation of the device filename must be GONE (the exact CORR-1 sink)
    assert "' <span class=\"meta\">' + d.file" not in tpl


def test_backups_has_a_single_subprocess_chokepoint():
    """backups.py must shell git through EXACTLY ONE subprocess.run chokepoint (`_git`). A SECOND subprocess.run
    git path would not be inspected by assert_no_git_write's chokepoint-call branch — so we pin the count at one
    (true today). Guards a future edit adding a second, unpinned git call (review MF-4)."""
    import ast as _ast
    src = open(os.path.join(ROOT, "scripts", "kontroll", "service", "backups.py"), encoding="utf-8").read()
    runs = [n for n in _ast.walk(_ast.parse(src))
            if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute) and n.func.attr == "run"]
    assert len(runs) == 1, "expected exactly one subprocess.run chokepoint, found %d" % len(runs)


def test_difflib_hunks_multi_hunk_reseed():
    """Two DISTANT changes produce TWO hunks, each re-seeding ol/nl from its OWN @@ header — guards cross-hunk
    counter drift (a refactor breaking per-hunk re-seed would still pass the single-hunk arithmetic test). Also
    pins the identical-input empty path (review CORR-7)."""
    a = ["L%d" % i for i in range(1, 41)]
    b = list(a)
    b[0] = "L1-changed"
    b[29] = "L30-changed"
    hunks, stats = backups._difflib_hunks(a, b)
    assert len(hunks) == 2 and stats == {"added": 2, "removed": 2}
    assert any(ln.get("nl") and ln["nl"] >= 27 for ln in hunks[1]["lines"])   # 2nd hunk re-seeds near line 30
    assert backups._difflib_hunks(a, a) == ([], {"added": 0, "removed": 0})   # identical → empty


@requires_git
def test_excluded_on_disk_file_cannot_be_viewed(tmp_captures):
    """The excluded (on-disk-only, history-excluded) FortiGate file has NO commit, so get_version raises for ANY
    valid-shaped rev → the route 400s. Proves the 'listed but NEVER rendered' claim at the SERVICE layer (not just
    the GUI's diffable gate): a direct /view hit can't serve the on-disk blob — there is no `git show working-tree`
    path, only `git show <rev>:<file>` over committed blobs (review CORR-12)."""
    rev = backups.list_versions("cisco_ios_192-0-2-2.cfg")["revisions"][0]["rev"]   # a valid 40-hex rev
    with pytest.raises(backups.BackupStoreError):
        backups.get_version("FortiGate_192-0-2-5_config.cfg", rev)


@requires_git
def test_get_version_truncates_oversized(tmp_captures, monkeypatch):
    """A capture longer than _MAX_VIEW_LINES is truncated (lines capped + truncated=True) rather than streamed whole
    — guards the view line-ceiling (a behaviour with no docstring'd test is a CLAUDE.md first-class gap; review
    CORR-13)."""
    monkeypatch.setattr(backups, "_MAX_VIEW_LINES", 2)
    rev = backups.list_versions("cisco_ios_192-0-2-2.cfg")["revisions"][0]["rev"]
    out = backups.get_version("cisco_ios_192-0-2-2.cfg", rev)
    assert out["truncated"] is True and len(out["lines"]) == 2


@requires_git
def test_list_versions_note_branches(tmp_captures):
    """list_versions surfaces the right NOTE: an excluded on-disk file → '…excluded from history' (revisions empty);
    an unknown file → raises (rejected by _log's allow-list, never a blank success). Guards the note the GUI shows
    for a non-diffable device + the bad-file rejection (review CORR-14)."""
    exc = backups.list_versions("FortiGate_192-0-2-5_config.cfg")
    assert exc["revisions"] == [] and "excluded" in (exc["note"] or "")
    with pytest.raises(backups.BackupStoreError):
        backups.list_versions("nonexistent_9-9-9-9.cfg")
