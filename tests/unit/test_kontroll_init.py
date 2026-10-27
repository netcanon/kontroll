"""scripts/kontroll-init.py — the from-scratch bootstrap CLI (the keygen split's local half). Tests the pure
helpers (which login secrets it generates, parsing the age public key) and that `main` never prints the control
PRIVATE key and is idempotent on a configured node. The heavy lifting (the .sops.yaml recipient write, the
dashboards encrypt) is the already-tested keygen.add_recipient + secrets service — here we mock those seams and
assert kontroll-init's ORCHESTRATION + the no-leak property.

Why each guards a real failure: generating a login password for a field that's ALREADY set would silently
clobber the operator's password; printing the control private key would defeat the whole encryption-at-rest
model; a non-idempotent init would churn secrets on every re-run.
"""
import importlib.util
import os
import shutil
import signal
import stat
import subprocess
import sys
import time

import pytest
import yaml

from kontroll import catalog, paths
from kontroll.service import keygen
from kontroll.service import secrets as secrets_service

from _age_recipient import bech32, x25519_recipient

pytestmark = pytest.mark.unit


def _load():
    """Load scripts/kontroll-init.py (hyphenated → not importable by name) as a module."""
    path = os.path.join(paths.ROOT, "scripts", "kontroll-init.py")
    spec = importlib.util.spec_from_file_location("kontroll_init", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ki = _load()


def test_parse_age_public_prefers_comment_then_falls_back():
    """The public recipient is read from the `# public key:` comment age-keygen writes, with a bare-age1
    fallback; junk yields None (so main fails clean rather than wiring a bogus recipient)."""
    assert ki._parse_age_public("# created: x\n# public key: age1abcpub000000000000000000000000\nAGE-SECRET-KEY-1FAKEZ\n") \
        == "age1abcpub000000000000000000000000"
    assert ki._parse_age_public("age1barebarebarebarebarebarebare00\n") == "age1barebarebarebarebarebarebare00"
    assert ki._parse_age_public("no key here") is None


def test_bootstrap_values_generates_only_absent_required_non_generate_fields():
    """kontroll-init supplies a login password ONLY for a required field that is absent AND has no `generate:`
    spec — it never regenerates an already-set field (clobber-safe) and never the API token (the service mints
    that). Against the real dashboards descriptor: with the API token already set, it supplies the two login
    passwords; with everything set, it supplies nothing."""
    form = catalog.secret_form("dashboards")
    keys = {f["key"] for f in form["fields"]}
    assert {"gui_admin_password", "grafana_admin_password", "kontroll_api_token"} <= keys

    supplied = ki.bootstrap_secret_values(form, existing={"kontroll_api_token"})
    assert set(supplied) == {"gui_admin_password", "grafana_admin_password"}      # the two login pws, generated
    assert "kontroll_api_token" not in supplied                                   # has generate: → service mints
    assert all(len(v) >= 16 for v in supplied.values())                          # strong

    assert ki.bootstrap_secret_values(form, existing=keys) == {}                  # all set → nothing to generate


def test_ensure_control_key_generates_when_absent_and_reads_public(tmp_path, monkeypatch):
    """When the key file is absent, ensure_control_key runs age-keygen (mocked to write a key file) and reads
    the public; created=True. When present, it does NOT regenerate (created=False) — the idempotence guard that
    protects an existing control key from being overwritten."""
    key_file = str(tmp_path / "age" / "keys.txt")

    def fake_keygen(cmd, **kw):
        # cmd == ["age-keygen", "-o", key_file] — write a fake identity file
        out = cmd[cmd.index("-o") + 1]
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write("# public key: age1faketestpubkey000000000000000000\nAGE-SECRET-KEY-1FAKEZZ\n")
        return type("P", (), {"returncode": 0})()
    monkeypatch.setattr(ki.subprocess, "run", fake_keygen)

    pub, created = ki.ensure_control_key(key_file)
    assert created is True and pub == "age1faketestpubkey000000000000000000"
    # second call: file exists → no regenerate
    monkeypatch.setattr(ki.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not regen")))
    pub2, created2 = ki.ensure_control_key(key_file)
    assert created2 is False and pub2 == pub


def test_main_idempotent_node_makes_no_change_and_never_prints_private(monkeypatch, capsys, tmp_path):
    """On a CONFIGURED node — key present, recipient present, dashboards already set — main returns 0, reports 'no
    change', and NEVER prints an AGE-SECRET-KEY. The control private key lives only in the 0600 file; the CLI must not
    echo it. The run is one on the operator's machine (KONTROLL_IN_CONTAINER unset and neither of the installer image's
    markers): in the installer container any key file but keys.txt in the mounted key directory is refused first, and
    the runner's default key path need not exist."""
    monkeypatch.delenv("KONTROLL_IN_CONTAINER", raising=False)
    monkeypatch.setattr(ki, "INSTALLER_IMAGE_MARKERS", (str(tmp_path / "no-installer-image"),))   # not the image
    monkeypatch.setattr(ki, "ensure_control_key", lambda kf: ("age1ctlpub00000000000000000000000000", False))
    monkeypatch.setattr(keygen, "add_recipient",
                        lambda pub, anchors: {"error": None, "changed": False, "paths": [".sops.yaml"],
                                              "recipient_domains": ["network", "proxmox", "dashboards"]})
    monkeypatch.setattr(secrets_service, "_existing_keys",
                        lambda d: {"gui_admin_password", "grafana_admin_password", "kontroll_api_token"})
    monkeypatch.setattr(secrets_service, "apply_secret_plan",
                        lambda plan: {"changed": False, "error": None, "paths": ["instance/secrets/dashboards.sops.yml"]})
    rc = ki.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "already present" in out and "no change" in out
    assert "AGE-SECRET-KEY" not in out


# --- --fresh: the brand-new-node path (scaffold instance/ + YOUR-key-only .sops.yaml) --------------------

MINTED = "age1freshtestpubkey00000000000000000000000000000000000000"   # the control key a fresh node mints

# A break-glass PUBLIC key made elsewhere: an X25519 recipient as age-keygen prints one, built so no real-length age
# key sits in the source for the leak guard, and ending in `p`, so a `q` added before that `p` keeps its checksum and
# only the length tells.
BG = x25519_recipient(ending="p")
OTHER = "age1othertestpubkey00000000000000000000000000000000000000"    # a key that is not this instance's control key
# An X25519 recipient holding an `l` in its data, with that `l` copied as `1`: bech32's split moves to the `1`, so
# the value is shaped as a plugin recipient whose checksum fails under the longer human part.
_WITH_L = next(k for k in (x25519_recipient(start=i) for i in range(1, 256)) if "l" in k[4:-6])
_L_AS_1 = _WITH_L[:_WITH_L.index("l", 4)] + "1" + _WITH_L[_WITH_L.index("l", 4) + 1:]
_BAD_PADDING = bech32("age", bytes([1]) * 32, pad=1)                    # a valid checksum, a padding bit set
BG_PRIVATE = "AGE-SECRET-KEY-1FAKEBREAKGLASSPRIVATE"                     # a minted one's private half (a fake)
_FRESH = ["--fresh", "--trust-mode", "separated", "--mgmt-ip", "198.51.100.7", "--domain", "lab.test"]


@pytest.fixture
def fresh_repo(tmp_path, monkeypatch):
    """A throwaway ROOT holding ONLY the shipped instance.example/ skeleton and NO instance/ overlay — the state a
    brand-new node clones into. --fresh scaffolds instance/ from it; ROOT is repointed so the run never touches the real
    tree, and the default control-key path is repointed under it so a run never reads the operator's own key file. age's
    own read of the key file (`_age_reads_public`) answers None, as on a machine without age-keygen, so a test's fake
    key file is not refused where age is installed; the tests of that check set it themselves. The run is one on the
    operator's machine (KONTROLL_IN_CONTAINER unset, which a runner's image could set, and the installer image's markers
    replaced by one path that is not there, as where the suite runs in that image); the test of the installer
    container's refusal sets each. Mirrors conftest.tmp_repo but for the fresh-install path."""
    shutil.copytree(os.path.join(paths.ROOT, "instance.example"), tmp_path / "instance.example")
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    monkeypatch.setattr(ki, "AGE_KEY_FILE", str(tmp_path / "age" / "keys.txt"))
    monkeypatch.setattr(ki, "_age_reads_public", lambda key_file: None)   # as where no age-keygen is installed
    monkeypatch.delenv("KONTROLL_IN_CONTAINER", raising=False)            # a run on the operator's machine
    monkeypatch.setattr(ki, "INSTALLER_IMAGE_MARKERS", (str(tmp_path / "no-installer-image"),))   # not the image
    return tmp_path


def _mock_fresh_seams(monkeypatch, minted=MINTED):
    """Stub the key-mint + dashboards-bootstrap I/O so a --fresh run exercises the REAL scaffold + REAL
    keygen.seed_fresh_sops without age/sops installed or touching ~/.config. The recipient write under test is
    deliberately NOT mocked — it is the security contract being verified."""
    monkeypatch.setattr(ki, "ensure_control_key", lambda kf: (minted, True))
    monkeypatch.setattr(secrets_service, "_existing_keys", lambda d: set())
    monkeypatch.setattr(secrets_service, "build_secret_plan", lambda d, s: {"error": None, "view": []})
    monkeypatch.setattr(secrets_service, "apply_secret_plan",
                        lambda p: {"changed": False, "error": None, "paths": []})


def test_fresh_writes_sops_with_only_this_instances_recipients(fresh_repo, monkeypatch, capsys):
    """SECURITY CONTRACT of --fresh: the scaffolded instance/.sops.yaml names ONLY this instance's recipients, in
    EVERY domain — the freshly-minted control key and the break-glass key the operator chose — and the shipped
    placeholder is REPLACED, never appended to. This is the guard against the inherit-recipients footgun the
    blind-Joe audit found: if a fresh node kept another instance's recipients, a stranger could decrypt the new
    node's secrets (and the new node's own secrets could be encrypted to a key it doesn't hold). Also asserts the
    control private key is never printed."""
    _mock_fresh_seams(monkeypatch)
    rc = ki.main(["--fresh", "--trust-mode", "separated", "--mgmt-ip", "198.51.100.7", "--domain", "lab.test",
                  "--break-glass-recipient", BG])
    assert rc == 0
    sops_path = os.path.join(fresh_repo, "instance", ".sops.yaml")
    assert os.path.exists(sops_path)
    text = open(sops_path, encoding="utf-8").read()
    recips = keygen._resolve_recipients(text)
    assert recips and all(s == {MINTED, BG} for s in recips.values())   # this instance's two keys, in every domain
    assert keygen.FRESH_PLACEHOLDER not in text                     # placeholder fully replaced (none left)
    assert "AGE-SECRET-KEY" not in capsys.readouterr().out


def test_fresh_scaffolds_skeleton_without_secrets_and_sets_instance_yml(fresh_repo, monkeypatch):
    """--fresh copies the instance.example/ skeleton into instance/ (instance.yml, fleet.yml, inventory, the
    homepage config) but NEVER the secrets/ subtree — a fresh node mints its own secrets, encrypted to its own
    key, and must not inherit ciphertext. The --mgmt-ip/--domain flags are written into instance/instance.yml so
    deploy-stack renders the right ${KONTROLL_MGMT_IP}/${KONTROLL_DOMAIN} (PR-2)."""
    _mock_fresh_seams(monkeypatch)
    assert ki.main(["--fresh", "--no-break-glass", "--trust-mode", "separated", "--mgmt-ip", "198.51.100.7", "--domain", "lab.test"]) == 0
    inst = os.path.join(fresh_repo, "instance")
    assert os.path.exists(os.path.join(inst, "instance.yml"))
    assert os.path.exists(os.path.join(inst, "fleet.yml"))
    assert os.path.exists(os.path.join(inst, "inventory", "hosts.yml"))
    assert os.path.exists(os.path.join(inst, "dashboards", "homepage", "services.yaml"))
    # the identifier-token list lands under its LIVE name (tests/_leak_guard.py reads instance/leak-tokens.txt), so
    # the operator fills in one file and the leak gate picks it up with no rename step
    assert os.path.exists(os.path.join(inst, "leak-tokens.txt"))
    assert not os.path.exists(os.path.join(inst, "leak-tokens.example.txt"))
    # the re-include file: under the root `/instance/*` rule it is what lets this node's canonical (and an instance
    # repository) track the overlay at all — without it `git add -A` commits no overlay and Semaphore reads the example
    assert os.path.exists(os.path.join(inst, ".gitignore"))
    # the deployment's own ledger template: a rename, or a place on the scaffold's skip list, would leave a new
    # node without it and nobody would notice
    assert os.path.exists(os.path.join(inst, "OPERATIONS.md"))
    assert not os.path.exists(os.path.join(inst, "secrets"))         # secret ciphertext is NEVER scaffolded
    iy = open(os.path.join(inst, "instance.yml"), encoding="utf-8").read()
    assert "mgmt_ip: 198.51.100.7" in iy and "domain: lab.test" in iy
    assert "192.0.2.10" not in iy                                    # placeholder value replaced


def test_fresh_personalizes_homepage_tiles(fresh_repo, monkeypatch):
    """--fresh --mgmt-ip X --domain Y fills the scaffolded homepage services.yaml's __MGMT_IP__/__DOMAIN__
    sentinels so a brand-new node's :3000 shows the REAL control-plane tiles on first boot. Guards the #72 stub
    leak the blind-Joe audit flagged: the shipped template pointed every tile at RFC-5737 TEST-NET (192.0.2.x) /
    example.com, so a fresh portal read as broken/leftover. Asserts no sentinel or placeholder survives in the
    personalized CONTROL-PLANE head and the real control-plane URLs (GUI :8443, Semaphore :3001, Grafana :3002,
    Prometheus :9090, API :8444, NPM control.<domain>) are present. The check is scoped to the head ABOVE the
    generated `# >>> kontroll fleet` markers: the generated Fleet block (#130) reflects the still-example inventory
    placeholders (192.0.2.x) until the operator edits inventory + redeploys — that's gen-homepage's domain, not
    kontroll-init's sentinel substitution. The Grafana tile carries the scheme Grafana SERVES (plain HTTP on
    :3002): the template said `https://` for it, so every fresh portal's Grafana tile was dead on first boot."""
    _mock_fresh_seams(monkeypatch)
    assert ki.main(["--fresh", "--no-break-glass", "--trust-mode", "separated", "--mgmt-ip", "198.51.100.7", "--domain", "lab.test"]) == 0
    hp = os.path.join(fresh_repo, "instance", "dashboards", "homepage", "services.yaml")
    text = open(hp, encoding="utf-8").read()
    head = text.split("# >>> kontroll fleet")[0]                     # the kontroll-init-personalized control-plane region
    for stale in ("__MGMT_IP__", "__DOMAIN__", "192.0.2", "example.com"):
        assert stale not in head                                     # no sentinel/placeholder left in the personalized head
    for tile in ("https://198.51.100.7:8443", "http://198.51.100.7:3001", "http://198.51.100.7:3002",
                 "http://198.51.100.7:9090", "https://198.51.100.7:8444", "https://control.lab.test"):
        assert tile in text                                         # the real control-plane board is rendered


def test_personalize_homepage_is_idempotent_and_clobber_safe(fresh_repo, monkeypatch):
    """_personalize_homepage is clobber-safe: a re-run after the sentinels are already filled (or after the
    operator hand-edited the file) finds no sentinels and changes NOTHING — matching the scaffold's never-overwrite
    posture. Guards a re-run of kontroll-init from re-substituting / corrupting an already-personalized portal."""
    _mock_fresh_seams(monkeypatch)
    assert ki.main(["--fresh", "--no-break-glass", "--trust-mode", "separated", "--mgmt-ip", "198.51.100.7", "--domain", "lab.test"]) == 0
    hp = os.path.join(fresh_repo, "instance", "dashboards", "homepage", "services.yaml")
    before = open(hp, encoding="utf-8").read()
    assert ki._personalize_homepage("9.9.9.9", "other.test") == []   # no sentinels remain → no tokens replaced
    assert open(hp, encoding="utf-8").read() == before               # file untouched (byte-identical)


def test_fresh_refuses_configured_overlay(fresh_repo, monkeypatch, capsys):
    """--fresh REFUSES (returns 1) once the overlay names a real recipient with no placeholder — protecting an
    already-configured node (the source instance, or an accidental second --fresh) from being re-initialised as
    brand-new. The guard fires BEFORE any scaffold/personalize write, so a configured tree is never mutated."""
    _mock_fresh_seams(monkeypatch)
    assert ki.main(["--fresh", "--no-break-glass", "--trust-mode", "separated"]) == 0                                 # first run seeds (placeholder -> minted)
    capsys.readouterr()
    rc2 = ki.main(["--fresh", "--no-break-glass", "--trust-mode", "separated"])                                       # second run: real recipient, no placeholder
    assert rc2 == 1 and "already names real recipients" in capsys.readouterr().err


# --- F8/F9 (dogfood): semaphore is an auto-mintable bootstrap domain; re-runs stay idempotent --------------

def test_bootstrap_values_honour_field_defaults():
    """F8 (live dogfood): a required field with a `default` (the Semaphore admin username/email — config, not a
    secret) is supplied that DEFAULT, never a random value; only a required field with neither generate: nor
    default is randomised (the admin password). This lets kontroll-init auto-mint the `semaphore` domain — which
    deploy-stack's .env render REQUIRES before the GUI can come up — with no operator prompt."""
    form = catalog.secret_form("semaphore")
    supplied = ki.bootstrap_secret_values(form, existing=set())
    assert supplied.get("semaphore_admin") == "admin"                        # descriptor default, not random
    assert supplied.get("semaphore_admin_email") == "admin@example.com"      # descriptor default
    assert "semaphore_db_password" not in supplied                           # generate: → the service mints it
    assert "semaphore_access_key_encryption" not in supplied                 # generate: → the service mints it
    assert len(supplied.get("semaphore_admin_password", "")) >= 16           # no default + no generate → random


def test_semaphore_is_a_bootstrap_domain():
    """F8: the `semaphore` domain joins `dashboards` in the auto-minted set (deploy-stack's .env needs both before
    the GUI exists); device-cred domains (proxmox, …) stay operator-provided and are NOT here."""
    assert "dashboards" in ki.BOOTSTRAP_DOMAINS and "semaphore" in ki.BOOTSTRAP_DOMAINS
    assert "proxmox" not in ki.BOOTSTRAP_DOMAINS


def test_fresh_skips_fully_provisioned_bootstrap_domain(fresh_repo, monkeypatch):
    """F9 (live dogfood): kontroll-init must be IDEMPOTENT — a bootstrap domain whose fields are ALL already set is
    SKIPPED (a fast no-op + clear log), never re-planned. Since #141 build_secret_plan KEEPS an already-set
    `generate:` field on a blank/no-`regenerate` call (it no longer unconditionally re-mints), so a re-run is
    idempotent either way; the skip stays as the explicit 'nothing to do' path. Asserts build_secret_plan is never
    called when both domains are fully provisioned."""
    monkeypatch.setattr(ki, "ensure_control_key", lambda kf: (MINTED, True))
    full = {d: {f["key"] for f in catalog.secret_form(d)["fields"]} for d in ki.BOOTSTRAP_DOMAINS}
    monkeypatch.setattr(secrets_service, "_existing_keys", lambda d: full.get(d, set()))
    planned = []
    monkeypatch.setattr(secrets_service, "build_secret_plan",
                        lambda d, s, r=None: planned.append(d) or {"error": None, "view": []})   # mirror the 3-arg sig
    monkeypatch.setattr(secrets_service, "apply_secret_plan",
                        lambda p: {"changed": False, "error": None, "paths": []})
    assert ki.main(["--fresh", "--no-break-glass", "--trust-mode", "separated", "--mgmt-ip", "1.2.3.4", "--domain", "x.test"]) == 0
    assert planned == []                                  # neither domain re-planned → no secret rotation


# --- F3 (trust_mode): the "never a silent default" rule enforced at BIRTH + the comment-tolerant write -------------
def test_fresh_requires_trust_mode(fresh_repo, monkeypatch, capsys):
    """F3 / the never-a-silent-default rule at the ONLY place a fresh overlay is born: `--fresh` WITHOUT
    `--trust-mode` exits 1 with a pointed message and writes NOTHING — kontroll never guesses whether you want a
    human-reviewed (separated) or an auto (solo) promote. Guards a fresh node silently defaulting its trust posture
    (the one setting that, unlike tls_mode, must fail LOUD not safe)."""
    _mock_fresh_seams(monkeypatch)
    rc = ki.main(["--fresh", "--no-break-glass", "--mgmt-ip", "198.51.100.7", "--domain", "lab.test"])      # no --trust-mode
    assert rc == 1
    assert "requires --trust-mode" in capsys.readouterr().err
    assert not os.path.exists(os.path.join(fresh_repo, "instance", "instance.yml"))   # refused BEFORE any scaffold


def test_fresh_writes_chosen_trust_mode_uncommenting_the_template(fresh_repo, monkeypatch):
    """F3: `--fresh --trust-mode solo` writes an ACTIVE `trust_mode: solo` into instance.yml by UNCOMMENTING the
    template's `# trust_mode: separated` line (the comment-tolerant substitution), so the operator's chosen posture
    is the durable, deploy-read value and no stale commented default survives. Guards the chosen posture silently
    not landing (which would then hard-fail deploy-stack's assert) or the wrong value persisting."""
    _mock_fresh_seams(monkeypatch)
    assert ki.main(["--fresh", "--no-break-glass", "--trust-mode", "solo", "--mgmt-ip", "198.51.100.7", "--domain", "lab.test"]) == 0
    iy = open(os.path.join(fresh_repo, "instance", "instance.yml"), encoding="utf-8").read()
    assert "trust_mode: solo" in iy                       # active, uncommented, the chosen value
    assert "trust_mode: separated" not in iy              # the commented template default is GONE (replaced)
    assert "# trust_mode:" not in iy                      # the assignment line is no longer commented


# --- T11 (S12): a break-glass recipient from an instance's first commit -------------------------------------------

def _rules():
    """{path_regex: recipients} of the scaffolded instance/.sops.yaml, read as keygen reads it (anchors expanded)."""
    return keygen._resolve_recipients(
        open(os.path.join(paths.ROOT, "instance", ".sops.yaml"), encoding="utf-8").read())


def _write_key(path, public):
    """An age identity file at `path` whose public half is `public` (the private line is a fake)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# public key: %s\nAGE-SECRET-KEY-1FAKEZZ\n" % public)


def _never(what):
    """A stub that fails the test if the step it replaces is reached."""
    def stub(*_a, **_k):
        pytest.fail("reached %s" % what)
    return stub


def test_fresh_requires_a_break_glass_choice(fresh_repo, monkeypatch, capsys):
    """`--fresh` with none of `--break-glass-recipient`, `--mint-break-glass` and `--no-break-glass` exits 1, names
    all three — the recipient made elsewhere first, as the preferred one — and writes nothing: no instance/, no key.
    Guards the tool silently creating an instance whose secrets one key opens (the state every instance was in
    before T11), and equally a silent mint: a default that printed a private key would put it in the log of every
    captured run."""
    _mock_fresh_seams(monkeypatch)
    monkeypatch.setattr(ki, "ensure_control_key", _never("the control key"))
    assert ki.main(_FRESH) == 1
    err = capsys.readouterr().err
    assert "--fresh requires a break-glass choice" in err and "preferred" in err
    assert err.index("--break-glass-recipient") < err.index("--mint-break-glass") < err.index("--no-break-glass")
    assert not os.path.exists(os.path.join(fresh_repo, "instance"))


def test_fresh_with_a_supplied_recipient_has_two_recipients_before_the_first_secret(fresh_repo, monkeypatch, capsys):
    """With `--break-glass-recipient`, at the moment each bootstrap secret is written every rule of
    instance/.sops.yaml already names exactly the control key and that recipient — the stubbed secret write reads
    the file when it is called. Guards the order: a recipient added after the first write leaves that file wrapped to
    one key, and a fresh install never runs the re-wrap that would fix it. The real seed and the real additive writer
    run; only the key mint and sops are stubbed."""
    _mock_fresh_seams(monkeypatch)
    seen = []
    monkeypatch.setattr(secrets_service, "apply_secret_plan",
                        lambda p: seen.append(_rules()) or {"changed": False, "error": None, "paths": []})
    assert ki.main(_FRESH + ["--break-glass-recipient", BG]) == 0
    assert len(seen) == len(ki.BOOTSTRAP_DOMAINS)
    assert all(rules and all(s == {MINTED, BG} for s in rules.values()) for rules in seen)
    out = capsys.readouterr().out
    assert "break-glass recipient: %s (ADDED" % BG in out and "AGE-SECRET-KEY" not in out


def test_minted_break_glass_is_shown_once_and_written_nowhere(fresh_repo, monkeypatch, capsys):
    """`--mint-break-glass` on a terminal: the minted private half appears exactly once on standard output, never on
    standard error, and in no file under the scratch root (the overlay, the key directory), on a line of its own with
    nothing before it; its public half is a recipient of every rule. Guards the private half reaching disk — where a
    backup, a snapshot or a commit would carry it — its being printed twice (a log line besides the block) or not at
    all (a recipient nobody holds), and its being indented: a key stored as the whole displayed line opens nothing
    (sops, age and age-keygen each refuse a key line that starts with spaces; T11 fold 4's read B, S2)."""
    _mock_fresh_seams(monkeypatch)
    monkeypatch.setattr(ki, "_stdout_is_terminal", lambda: True)
    monkeypatch.setattr(keygen, "generate_keypair", lambda: {"public": BG, "private": BG_PRIVATE})
    assert ki.main(_FRESH + ["--mint-break-glass"]) == 0
    out, err = capsys.readouterr()
    assert out.count(BG_PRIVATE) == 1 and BG_PRIVATE not in err and BG_PRIVATE in out.splitlines()
    assert all(s == {MINTED, BG} for s in _rules().values())
    for directory, _dirs, names in os.walk(fresh_repo):
        for name in names:
            with open(os.path.join(directory, name), "rb") as fh:
                assert BG_PRIVATE.encode() not in fh.read(), os.path.join(directory, name)


def test_the_private_half_is_shown_even_when_a_later_step_fails(fresh_repo, monkeypatch, capsys):
    """When the bootstrap-secret step fails after the minted recipient was written (sops absent, say), kontroll-init
    exits 1 — and the private half was still shown, once, before the failure. Guards a half-finished init leaving a
    recipient in .sops.yaml whose private half nobody was shown: a count of two and a recovery of zero."""
    _mock_fresh_seams(monkeypatch)
    monkeypatch.setattr(ki, "_stdout_is_terminal", lambda: True)
    monkeypatch.setattr(keygen, "generate_keypair", lambda: {"public": BG, "private": BG_PRIVATE})
    monkeypatch.setattr(secrets_service, "apply_secret_plan",
                        lambda p: {"changed": False, "error": "sops: not found", "paths": []})
    assert ki.main(_FRESH + ["--mint-break-glass"]) == 1
    out, err = capsys.readouterr()
    assert out.count(BG_PRIVATE) == 1 and "could not write the dashboards domain" in err
    assert all(BG in s for s in _rules().values())


def test_no_break_glass_is_explicit_and_says_what_it_costs(fresh_repo, monkeypatch, capsys):
    """`--no-break-glass` creates the instance with one recipient per rule, the control key, and says what that
    costs: the key file whose loss destroys every secret, R-KEY-1, and the document that adds a recipient later.
    Guards the opt-out turning into a quiet default — fine for the scratch instance it exists for, and the state
    every instance was in before T11 for one that took it without reading why."""
    _mock_fresh_seams(monkeypatch)
    assert ki.main(_FRESH + ["--no-break-glass"]) == 0
    assert all(s == {MINTED} for s in _rules().values())
    out = capsys.readouterr().out
    assert "NO break-glass recipient" in out and "R-KEY-1" in out and "docs/break-glass.md" in out
    assert "Losing %s destroys every secret" % ki.AGE_KEY_FILE in out


@pytest.mark.parametrize("recipient,expect", [
    (BG_PRIVATE, "was given a PRIVATE key"),
    (" " + BG_PRIVATE.lower(), "was given a PRIVATE key"),
    ("age1short", "is not an age public key"),
    ("age1BREAKGLASSUPPERCASEPUB000000000000000000000000000000", "is not an age public key"),
    (BG + "," + MINTED, "is not an age public key"),
    (BG[:-1] + "q", "is not a valid age public key"),
    (BG[:20] + "b" + BG[21:], "is not a valid age public key"),
    (BG[:-1] + "qp", "is not a valid age public key"),
    (_L_AS_1, "is not a valid age public key"),
    (BG[:10] + "1" + BG[10:], "is not a valid age public key"),
    (_BAD_PADDING, "is not a valid age public key"),
    (bech32("age1yubikey", bytes(range(33))), "is a plugin or post-quantum age recipient"),
    (bech32("age1fido2-hmac", bytes(range(33))), "is a plugin or post-quantum age recipient"),
    (bech32("age1pq", bytes(range(64))),
     ("is a plugin or post-quantum age recipient",
      "needs a sops whose age knows it on every machine that encrypts or decrypts")),
    (bech32("age1ab!cd", bytes(range(1, 34))), "is not an age public key"),
    (bech32("age", bytes(32)), "is a low-order X25519 point"),
    (bech32("age", (1).to_bytes(32, "little")), "is a low-order X25519 point"),
], ids=["private-key", "private-key-lower-padded", "too-short", "upper-case", "two-joined", "checksum-typo",
        "outside-the-alphabet", "q-before-final-p", "an-l-copied-as-1", "a-1-added", "non-zero-padding",
        "a-plugin-recipient", "a-plugin-name-with-a-hyphen", "a-post-quantum-recipient",
        "a-plugin-name-outside-its-set", "the-all-zero-point", "the-point-u-1"])
def test_a_malformed_or_private_recipient_is_refused_before_any_write(fresh_repo, monkeypatch, capsys,
                                                                     recipient, expect):
    """A `--break-glass-recipient` that is a PRIVATE key, or is not an age public key as age-keygen prints one, exits 1
    before anything is written, and the value is not echoed back; a private key in lower case or with a space before it
    is still named as one; a valid key with more after it (two keys joined by a comma) is refused whole; a value of the
    right shape whose bech32 checksum fails — one character changed, or one outside the bech32 alphabet — is refused as
    one, and so is a `q` added before the final `p`, which the checksum cannot see and the length can; an `l` copied as
    `1`, or a `1` added, which moves bech32's split so the value is shaped `age1<name>1…`, is refused as a typo too, not
    as a plugin recipient; and one whose four padding bits are not zero (a value no age-keygen printed, with a valid
    checksum) as not valid. A plugin or post-quantum recipient (`age1<name>1…`, such as a hardware token's, its name
    holding a `-` as age allows) is refused as one, the post-quantum reason naming every machine that encrypts or
    decrypts; a name holding a character outside age's plugin set (`age1ab!cd1…`) is refused as no age key; and so is a
    low-order X25519 point (the all-zero key, u = 1); the post-quantum row's 64 bytes are no real key, since the check
    reads the name and the checksum. Guards a typo creating a recipient nobody can decrypt with (sops refuses it only at
    the first encrypt, after the instance and its .sops.yaml are written, so the run fails half-way with an error that
    blames the age key and no re-run repairs it), and the same for a padding bit (age and sops: "non-zero padding"), a
    plugin recipient (sops encrypts to one only where `age-plugin-<name>` is installed, and kontroll's images ship
    none), a post-quantum one (it needs a sops whose age knows it on every machine that encrypts or decrypts) and a
    low-order point (age refuses it: "low order point") — T11 fold 4's read A, S1; an `l` read as `1` told to make a new
    key, and a plugin name with a `-` told it is no age key at all (fold 5's reads, 70 N1 and N3, 80 N1 and S2); a
    post-quantum recipient given a false reason, and a name outside that set given the plugin message (fold 6's read 90,
    N5); a check that reads only a valid prefix, a private key pasted by mistake being printed to the terminal or its
    log a second time, and a lower-cased or padded one losing the advice that names it a private key."""
    _mock_fresh_seams(monkeypatch)
    monkeypatch.setattr(ki, "ensure_control_key", _never("the control key"))
    assert ki.main(_FRESH + ["--break-glass-recipient", recipient]) == 1
    err = capsys.readouterr().err
    expects = expect if isinstance(expect, tuple) else (expect,)
    assert all(e in err for e in expects) and recipient not in err and "Nothing was written" in err
    assert not os.path.exists(os.path.join(fresh_repo, "instance"))


def test_the_recipient_check_holds_the_human_part_and_the_x25519_length():
    """`_is_age_recipient` accepts an X25519 recipient (32 bytes: 58 data characters, zero padding bits), and refuses a
    plugin recipient even at that length (human part `age1<name>`, which `_is_plugin_recipient` names), BIP-173's own
    valid vector `a12uel5l` (its checksum is right, its human part is not `age`), an X25519 recipient with a `q` added
    before its final `p` (which the checksum cannot see), a 31-byte one; and a 32-byte one (`bytes([1]) * 32`) that
    passes, refused once any one of its four padding bits is set; `_is_plugin_recipient` names a plugin recipient whose
    name holds a `-`, and not an X25519 recipient with an `l` copied as `1`. Guards a recipient sops refuses ("invalid
    X25519 public key", "non-zero padding", or a plugin it cannot start) passing the check, so the run fails at the
    first encrypt after .sops.yaml is written (T11 fold 4's read A, S1: fold 4 accepted a plugin recipient here; fold
    5's reads, 70 N3 and 80 S2: fold 5 accepted a padding bit); a typo named a plugin recipient (70 N1, 80 N1); and the
    human-part clause, which no caller reaches today (the plugin refusal comes first), being dropped unnoticed."""
    assert BG.endswith("p") and len(BG) == 62 and ki._is_age_recipient(BG)
    plugin = bech32("age1yubikey", bytes(range(1, 33)))
    assert not ki._is_age_recipient(plugin) and ki._is_plugin_recipient(plugin) and not ki._is_plugin_recipient(BG)
    assert ki._is_plugin_recipient(bech32("age1fido2-hmac", bytes(range(1, 33))))
    assert not ki._is_plugin_recipient(_L_AS_1) and not ki._is_age_recipient(_L_AS_1)
    assert not ki._is_age_recipient("a12uel5l")
    assert not ki._is_age_recipient(BG[:-1] + "qp")
    assert not ki._is_age_recipient(bech32("age", bytes(31)))
    assert ki._is_age_recipient(bech32("age", bytes([1]) * 32))
    assert not any(ki._is_age_recipient(bech32("age", bytes([1]) * 32, pad=bit)) for bit in (1, 2, 4, 8))


def test_the_low_order_check_agrees_with_x25519_itself():
    """`_x25519_low_order` is True exactly where X25519 itself (the `cryptography` package's, OpenSSL's) refuses the
    point as of small order: over every 32-byte spelling of a small-order point (0, 1, p - 1 and the two of order 8;
    p and p + 1, the only other values below 2^255 that age reduces mod p to one of them; and each of those with the
    top bit set, which RFC 7748 masks), and over seeded random keys; and `x25519_recipient`'s keys are not low-order.
    The oracle is tests/requirements-dev.txt's `cryptography`, imported plainly, so a machine without it fails here
    rather than skipping. Guards the pure-Python ladder (the check needs no package the installer image lacks)
    drifting from the curve: a refusal of a key age takes, or a pass of one age refuses at the first encrypt (T11
    fold 4's read A, S1); and the test skipping unseen where the package is absent, as it did in CI's `tests` jobs
    (fold 5's read 80, N2)."""
    from cryptography.hazmat.primitives.asymmetric import x25519
    import random
    p = 2 ** 255 - 19
    small = [0, 1, p - 1, 325606250916557431795983626356110631294008115727848805560023387167927233504,
             39382357235489614581723060781553021112529911719440698176882885853963445705823]
    encodings = [u.to_bytes(32, "little") for u in small + [u + p for u in small if u + p < 2 ** 255]]
    encodings += [e[:31] + bytes([e[31] | 0x80]) for e in encodings]
    spellings = len(encodings)
    rng = random.Random(7748)
    encodings += [bytes(rng.getrandbits(8) for _ in range(32)) for _ in range(200)]
    me = x25519.X25519PrivateKey.from_private_bytes(bytes(range(32)))

    def refused(raw):
        try:
            me.exchange(x25519.X25519PublicKey.from_public_bytes(raw))
        except ValueError:
            return True
        return False

    assert [refused(e) for e in encodings] == [True] * spellings + [False] * 200   # the oracle: those, no other
    assert [ki._x25519_low_order(bech32("age", e)) for e in encodings] == [refused(e) for e in encodings]
    assert not any(ki._x25519_low_order(x25519_recipient(start=i)) for i in range(4))


def test_a_recipient_equal_to_the_control_key_is_refused(fresh_repo, monkeypatch, capsys):
    """A break-glass recipient that IS this node's control key is refused before anything is written when the key file
    already exists, and, as a belt, at the write step when the key was minted in this same run — so the copy is never
    added. Guards a copy of the control key counted as a second recipient: the rule would name one key twice, and
    losing it would still destroy every secret."""
    _mock_fresh_seams(monkeypatch)
    _write_key(ki.AGE_KEY_FILE, BG)
    monkeypatch.setattr(ki, "ensure_control_key", _never("the control key"))
    assert ki.main(_FRESH + ["--break-glass-recipient", BG]) == 1
    assert "is this node's control key" in capsys.readouterr().err
    assert not os.path.exists(os.path.join(fresh_repo, "instance"))
    os.remove(ki.AGE_KEY_FILE)                         # the belt: nothing to compare before the run mints the key
    _mock_fresh_seams(monkeypatch, minted=BG)
    assert ki.main(_FRESH + ["--break-glass-recipient", BG]) == 1
    assert "is this node's control key" in capsys.readouterr().err
    assert all(s == {BG} for s in _rules().values())   # the control recipient alone; the copy was not added


def _existing_instance(fresh_repo, monkeypatch):
    """An instance created with one recipient, then a node whose control key file is where kontroll-init looks and
    whose bootstrap domains are fully set — the state an existing instance is in. Returns its .sops.yaml path."""
    _mock_fresh_seams(monkeypatch)
    assert ki.main(_FRESH + ["--no-break-glass"]) == 0
    _write_key(ki.AGE_KEY_FILE, MINTED)
    monkeypatch.setattr(ki, "ensure_control_key", lambda kf: (ki._read_public(kf), False))
    full = {d: {f["key"] for f in catalog.secret_form(d)["fields"]} for d in ki.BOOTSTRAP_DOMAINS}
    monkeypatch.setattr(secrets_service, "_existing_keys", lambda d: full.get(d, set()))
    return os.path.join(fresh_repo, "instance", ".sops.yaml")


def test_existing_instance_adds_the_recipient_additively_and_names_the_re_wrap(fresh_repo, monkeypatch, capsys):
    """Without --fresh, `--break-glass-recipient` adds the key to every rule — under BOTH recipient anchors, so the
    operational domains (network, proxmox) get it too — removes no line of .sops.yaml, and prints the re-wrap
    (`keygen.SOPS_UPDATEKEYS`) and the runbook; a second run changes nothing and asks for no re-wrap. Guards the shell
    procedure for an existing instance (docs/break-glass.md) under-granting the operational domains, as a hand edit
    under one anchor does, or dropping a recipient the existing ciphertext is wrapped to."""
    sops_path = _existing_instance(fresh_repo, monkeypatch)
    before = open(sops_path, encoding="utf-8").read()
    capsys.readouterr()
    assert ki.main(["--break-glass-recipient", BG]) == 0
    after = open(sops_path, encoding="utf-8").read()
    assert all(s == {MINTED, BG} for s in _rules().values())
    assert set(before.splitlines()) <= set(after.splitlines())      # only added: every line of the old file is kept
    out = capsys.readouterr().out
    assert keygen.SOPS_UPDATEKEYS in out and "docs/break-glass.md" in out
    assert ki.main(["--break-glass-recipient", BG]) == 0
    out = capsys.readouterr().out
    assert "already present" in out and keygen.SOPS_UPDATEKEYS not in out
    assert open(sops_path, encoding="utf-8").read() == after


def test_a_recipient_without_fresh_and_no_control_key_is_refused_and_mints_nothing(fresh_repo, monkeypatch, capsys):
    """Without --fresh, `--break-glass-recipient` where no control key file exists exits 1 before anything is written:
    .sops.yaml is unchanged, no key file is created, and the message names the installer shell where the key is.
    Guards the non-fresh path minting a NEW control key (its step 1) and adding it to every domain before it reaches
    the break-glass step — an unrequested recipient whose private half sits wherever the command happened to run,
    such as a shell without the key directory's bind."""
    sops_path = _existing_instance(fresh_repo, monkeypatch)
    os.remove(ki.AGE_KEY_FILE)
    before = open(sops_path, encoding="utf-8").read()
    monkeypatch.setattr(ki, "ensure_control_key", _never("the control key"))
    capsys.readouterr()
    assert ki.main(["--break-glass-recipient", BG]) == 1
    err = capsys.readouterr().err
    assert "no control key" in err and "kontroll-installer.sh shell" in err
    assert open(sops_path, encoding="utf-8").read() == before and not os.path.exists(ki.AGE_KEY_FILE)


@pytest.mark.parametrize("text,want", [
    ("creation_rules:\n  - path_regex: a\n    key_groups:\n      - age: [%(c)s, %(b)s]\n", True),
    ("creation_rules:\n  - path_regex: a\n    age: %(b)s,%(c)s\n", True),
    ("creation_rules:\n  - path_regex: a\n    key_groups:\n      - age: ['%(b)s,%(c)s']\n", True),
    ("creation_rules:\n  - path_regex: a\n    key_groups:\n      - age: [%(b)s]\n      - age: [%(c)s]\n", True),
    ("creation_rules:\n  - path_regex: a\n    key_groups:\n      - age: [%(c)s]\n  - path_regex: b\n    key_groups:\n"
     "      - pgp: [F00]\n", True),
    ("creation_rules:\n  - path_regex: a\n    key_groups:\n      - age: [%(c)s]\n  - path_regex: b\n    key_groups:\n"
     "      - age: [%(b)s]\n", False),
    ("creation_rules:\n  - path_regex: a\n    age: %(b)s\n", False),
    ("creation_rules:\n  - path_regex: a\n    age: %(c)s\n    key_groups:\n      - age: [%(b)s]\n", False),
    ("creation_rules: [\n", True),
    (None, True),
], ids=["key-group-list", "rule-level-string", "comma-in-a-list-item", "second-group", "no-age-rule-skipped",
        "one-rule-without-it", "rule-level-without-it", "key-groups-and-rule-level", "unparsable", "no-file"])
def test_the_control_key_check_reads_every_recipient_form(fresh_repo, text, want):
    """`_control_everywhere` finds the control key in a rule whichever form names it — a key-group list, a
    comma-separated string on the rule itself, a list item holding a comma, any of several groups — skips a rule
    with no age recipient, and is False when one rule that names age recipients lacks it, in a key group or on the
    rule itself — or only on the rule itself beside key groups, where sops encrypts to the groups and ignores it;
    with no .sops.yaml, or one that does not parse, it defers to the write step, which refuses on its own. Guards the
    control-key refusal turning away a real control key written in a form the reader misses, passing a key that one
    rule does not name — a reader blind to the rule's own `age:` saw no recipients there and passed any key, and one
    that read it beside key groups passed a key sops never encrypts to — and a malformed file stopping the run with a
    traceback."""
    path = os.path.join(fresh_repo, "instance", ".sops.yaml")
    if text is not None:
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text % {"c": MINTED, "b": BG})
    assert ki._control_everywhere(MINTED) is want


def test_a_key_file_that_is_not_the_control_recipient_is_refused(fresh_repo, monkeypatch, capsys):
    """Without --fresh, `--break-glass-recipient` where the key file kontroll-init reads holds a key that is not a
    recipient of every rule in instance/.sops.yaml exits 1, names the key file, and leaves .sops.yaml byte-identical.
    Guards a `--key-file` naming the wrong file, or a keys.txt whose first `# public key:` line is not the control
    key, adding THAT key to every domain in step 2 beside the break-glass one — after the re-wrap its holder, whoever
    it is, opens every secret; only the operator's reading of step 3's dry run (an added key other than the
    break-glass one) would stop it."""
    sops_path = _existing_instance(fresh_repo, monkeypatch)
    before = open(sops_path, encoding="utf-8").read()
    _write_key(ki.AGE_KEY_FILE, OTHER)
    capsys.readouterr()
    assert ki.main(["--break-glass-recipient", BG]) == 1
    err = capsys.readouterr().err
    assert "is not a recipient of every rule" in err and ki.AGE_KEY_FILE in err and "Nothing was written" in err
    assert OTHER not in err
    assert open(sops_path, encoding="utf-8").read() == before


def _key_file_as(kind):
    """Put at the key file's path what `kind` names: a bare private line, an empty file, a directory, a link to
    nothing, a FIFO, or a Latin-1 file (a private line after a byte that is not UTF-8)."""
    kf = ki.AGE_KEY_FILE
    os.makedirs(os.path.dirname(kf), exist_ok=True)
    if os.path.isfile(kf):
        os.remove(kf)
    if kind == "a-directory":
        os.makedirs(kf)
        return
    if kind == "a-link-to-nothing":
        os.symlink(kf + ".gone", kf)
        return
    if kind == "a-fifo":
        os.mkfifo(kf)
        return
    body = {"the-private-line-only": BG_PRIVATE.encode() + b"\n", "empty": b"",
            "not-utf-8": b"# cr\xe9\xe9e\n" + BG_PRIVATE.encode() + b"\n"}[kind]
    with open(kf, "wb") as fh:
        fh.write(body)


_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="a symbolic link or a FIFO at the key file's path")
_FRESH_CHOICES = {"fresh-no-break-glass": ["--no-break-glass"], "fresh-mint": ["--mint-break-glass"],
                  "fresh-recipient": ["--break-glass-recipient", BG]}


@pytest.mark.parametrize("kind", ["the-private-line-only", "empty", "a-directory", "not-utf-8",
                                  pytest.param("a-link-to-nothing", marks=_POSIX_ONLY),
                                  pytest.param("a-fifo", marks=_POSIX_ONLY)])
@pytest.mark.parametrize("run", ["a-recipient-without-fresh"] + list(_FRESH_CHOICES))
def test_a_key_file_without_its_public_line_is_named_as_such(fresh_repo, monkeypatch, capsys, request, run, kind):
    """A run that reads the control key file — `--break-glass-recipient` without --fresh, or a `--fresh` run with any of
    the three choices where something is at the key file's path — exits 1 before anything is written when kontroll-init
    cannot read a `# public key:` line from it (the file holds only the `AGE-SECRET-KEY-` line, is empty, is a
    directory, a link to nothing or a FIFO, or is not UTF-8), says so and how to add one, does not call the key absent,
    mints nothing, and never echoes the private line: no instance/ on the fresh path, an unchanged .sops.yaml on the
    other. Guards the refusal sending the operator to look for a control key that is there (sops opens every domain with
    such a file), as "no control key at …" did; a `--fresh` run scaffolding instance/ before it fails to read the key
    (T11 fold 4's read A, N7), for the choice that prints a private key too (fold 5's read 80, N3: only
    `--no-break-glass` was run); a link to nothing read as no file, so `--fresh` scaffolded instance/ and died in a
    traceback from age-keygen, and a FIFO opened, which hung the run (read 70, N4), a regression there now failing
    within 30 s rather than holding a shared runner for hours (fold 6's read 100, N2); a file it cannot read named as
    one missing a line, with a remedy that fails the same way (read B, N7); and a not-UTF-8 file ending the run in a
    traceback (read A, N5)."""
    if kind == "a-fifo":
        def _hung(*_):
            raise AssertionError("kontroll-init opened the FIFO and blocked")
        # None: a handler set in C, which Python cannot put back; the default is the nearest it can restore
        handler = signal.signal(signal.SIGALRM, _hung) or signal.SIG_DFL
        (left, every), armed = signal.setitimer(signal.ITIMER_REAL, 30), time.monotonic()

        def _restore():
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, handler)
            if left:   # a runner's own timer, given back less the time these rows took, its interval kept
                signal.setitimer(signal.ITIMER_REAL, max(0.01, left - (time.monotonic() - armed)), every)
        request.addfinalizer(_restore)
    if run in _FRESH_CHOICES:
        _mock_fresh_seams(monkeypatch)
        monkeypatch.setattr(ki, "ensure_control_key", _never("the control key"))
        monkeypatch.setattr(ki, "_stdout_is_terminal", lambda: True)
        monkeypatch.setattr(keygen, "generate_keypair", _never("the mint"))
        _key_file_as(kind)
        assert ki.main(_FRESH + _FRESH_CHOICES[run]) == 1
    else:
        sops_path = _existing_instance(fresh_repo, monkeypatch)
        before = open(sops_path, encoding="utf-8").read()
        _key_file_as(kind)
        capsys.readouterr()
        assert ki.main(["--break-glass-recipient", BG]) == 1
        assert open(sops_path, encoding="utf-8").read() == before
    err = capsys.readouterr().err
    assert "could not read a `# public key:` line from" in err and "age-keygen -y" in err
    assert "Nothing was written" in err and "no control key" not in err and BG_PRIVATE not in err
    if run in _FRESH_CHOICES:
        assert not os.path.exists(os.path.join(fresh_repo, "instance"))


@pytest.mark.parametrize("age_reads,refused", [(OTHER, True), ("", True), (MINTED, False)],
                         ids=["another-keys-line", "age-reads-no-key", "age-agrees"])
@pytest.mark.parametrize("run", ["a-recipient-without-fresh", "fresh"])
def test_a_key_file_age_reads_otherwise_is_refused(fresh_repo, monkeypatch, capsys, run, age_reads, refused):
    """When the `# public key:` line kontroll-init reads from the key file is not what age itself derives from that file
    (`age-keygen -y` names another key, or reads no key there: a byte-order mark before the key, a file that holds only
    a public line), a `--fresh --no-break-glass` run and a `--break-glass-recipient` run on an existing instance each
    exit 1 before anything is written, name the file and give the `age-keygen -y` that prints what age reads, with the
    reason that holds in every such case (kontroll-init cannot confirm that the line names the file's key, and the
    control-key check would rest on the line alone), and name a broken age-keygen as a cause whose remedy keeps the
    check (fix it, or run it through the installer: `fresh-init` for a --fresh run, `shell` otherwise, with the key at
    ~/.config/sops/age/keys.txt and no --key-file, the one file every installer verb reads), never one that removes it;
    where age agrees, each run goes on. Guards an instance written to a key that cannot open it: kontroll-init took the
    file's public text on trust, so such a file gave rc 0 and secrets its control key could not decrypt (T11 fold 5's
    read 70, N5); and a reason false in a case it is given (an existing instance's re-wrap "would fail" with a file
    whose key is another recipient's; fold 7's reads 110 and 111, N1); a reason untrue where age names another key,
    which kontroll-init then knows (fold 8's read 121, N1); the tool-fault cause and remedy held by no row (its N2), and
    a fault the cause leaves out (an age-keygen that runs and fails: read 120, N5); a remedy, "fix or remove it", whose
    second half skips the check, so a misread file was then taken on its line (read 120, S1), and that advice added back
    beside the warning, which every row passed (fold 9's read 131, N3); a remedy that sent a --fresh run to the
    installer shell, which leaves instance/ root's where `fresh-init` hands it back (read 131, N5), and a --key-file of
    this machine's into the container, where it names nothing, so a --fresh run minted a key there that the container
    discarded at exit (read 130, N1); and "removing it does not fix this", untrue where another age-keygen on PATH runs
    (read 130, N2); and fold 10's "with the key file in ~/.config/sops/age, the directory of keys it mounts: a
    --key-file naming another path of this machine names nothing there", which reads as if a --key-file into that
    directory named something there, so an operator who kept the flag the refusal invites minted a key the container
    discarded (fold 10's reads 140 and 141, S1); and fold 11's "another file of that directory is
    /root/.config/sops/age/<name>", a file no installer verb reads (fold 11's read 151, S1)."""
    if run == "fresh":
        _mock_fresh_seams(monkeypatch)
        _write_key(ki.AGE_KEY_FILE, MINTED)
        monkeypatch.setattr(ki, "_age_reads_public", lambda key_file: age_reads)
        rc = ki.main(_FRESH + ["--no-break-glass"])
        wrote = os.path.exists(os.path.join(fresh_repo, "instance"))
    else:
        sops_path = _existing_instance(fresh_repo, monkeypatch)
        before = open(sops_path, encoding="utf-8").read()
        monkeypatch.setattr(ki, "_age_reads_public", lambda key_file: age_reads)
        capsys.readouterr()
        rc = ki.main(["--break-glass-recipient", BG])
        wrote = open(sops_path, encoding="utf-8").read() != before
    err = capsys.readouterr().err
    if refused:
        assert rc == 1 and not wrote, err
        assert "is not the public half of the key age reads" in err and "age-keygen -y %s" % ki.AGE_KEY_FILE in err
        assert "cannot confirm that the line names the file's key" in err and "rest on the line alone" in err
        assert "age-keygen gave no answer" in err and "fix it, or run this through the installer" in err
        assert "fresh-init …` for a --fresh run" in err and "kontroll-installer.sh shell` otherwise" in err
        assert "with the key at ~/.config/sops/age/keys.txt and no --key-file" in err
        assert "so a --key-file as this machine spells it names nothing there" in err
        assert "every installer verb reads that one file" in err and "<name>" not in err
        assert "naming another path of this machine" not in err   # fold 10's clause: that directory's files named too
        assert "(it cannot run here, ran past 60 s, failed, or printed what is not UTF-8)" in err
        assert "fixes this only where another age-keygen on PATH runs" in err and "takes its line as written" in err
        assert "or remove it" not in err                 # no remedy that switches the check off
        assert "Nothing was written" in err
    else:
        assert rc == 0 and wrote, err


@_POSIX_ONLY
@pytest.mark.parametrize("run", ["fresh", "existing"])
def test_the_installer_container_makes_a_key_only_where_it_mounts_keys(fresh_repo, monkeypatch, capsys, tmp_path, run):
    """In the installer container (KONTROLL_IN_CONTAINER set to any non-empty value, "0" included, or one of the image's
    markers with the variable gone, as a `sudo` or an `env -i` in its shell leaves it: the marker the Dockerfile makes,
    or the dispatcher every installer image carries), a `--fresh` run and a run on an existing instance exit 1 before
    anything is read or written (no key made, no directory created, no instance/ scaffolded, .sops.yaml unchanged) when
    the key file is not the default key file in the directory the installer mounts for keys: a missing one in a
    directory the container lacks (a --key-file as the operator's machine spells it, `keys.txt` too, whose directory the
    guard cannot stat), in another directory of the container, a bare name read from another working directory, a path
    that reads as that directory but leads out through a link, or another name in that directory, which no installer
    verb reads; the default key file under a HOME other than the installer's (its key directory present); and a file
    that is there already, of another name (in that directory or elsewhere) or a keys.txt in another directory, which
    the run would key the instance to; and a path that is no regular file (in that directory, a link to no file, a link
    to a directory, a FIFO, a link to a FIFO and a socket; the directory itself; and `/dev/null`), even on the run that
    only reads (there is no key to read). The refusal names the file, what made kontroll-init take the run for the
    installer's (the variable, then each marker that is there: all three with the variable and both markers there, as in
    the image as built), that directory and the remedy (no --key-file, through `fresh-init` for a --fresh run; on a
    host, undo what the parenthesis names), and comes before the break-glass refusal and a break-glass mint. The default
    key file, however the path spells its directory (through a link too), goes on, and so does `keys.txt` itself under
    another name (a link to it); so do a file of another name or in another directory that an existing instance's
    `--break-glass-recipient` only reads, and the same run on the operator's machine, where step 1 makes a bare-named
    key in the working directory. Guards the route fold 10's remedy printed: `fresh-init --key-file
    ~/.config/sops/age/<name>`, expanded on the host to a home the container does not have, made a key in a directory
    step 1 created in the container's own layer, printed it as the key to keep and wrote the instance to it with rc 0,
    and the container discarded it at exit (T11 fold 10's reads 140 and 141, S1); and fold 11's remedy, `<that
    directory>/<name>`, which gave an rc-0 instance keyed to a root-owned file no installer verb reads, while the next
    documented step minted a keys.txt that cannot open it (fold 11's read 151, S1); a guard comparing the directory's
    spelling, not what the kernel opens, and one moved after the break-glass mint, which printed a key no instance would
    name, each passed every row (its N2, N3); and a guard keyed on the variable's value "1" and on HOME, which `sudo`,
    `env -i`, another value or a moved HOME turned off, and step 1's crash on a bare name the guard lets through, which
    a stubbed step 1 hid (fold 11's read 150, N2, N3); and a guard reading "1" or "true" only, or comparing against
    HOME's key directory, which every row passed; the published image, which lacks the marker, where `sudo` or `env -i`
    turned the guard off; and a file of another name that was there already, which keyed a --fresh instance to a file no
    installer verb reads, or was added to every rule (fold 12's reads 160, N1, N4 and 161, N1, N3, N4); and a guard that
    passes `keys.txt` when it cannot stat the directory, which no row held once fold 13 rewrote the rows, and a guard
    that takes a link to no file as a file that is there, which lets the run that only reads past it to a later check
    and refuses the other runs as if a key file were there (fold 13's own mutants); and one that lets through any
    keys.txt that is there, which keys a --fresh instance to one in the container's own layer (fold 13's read 171, N2);
    and a refusal that did not say which of the variable or a marker fired, so a host with a file at a marker path was
    refused as the installer with no reason it could act on, and wordings that told a run with no file that it only
    reads it, a run without --fresh that it would key the instance, and a link to keys.txt that it is a key file no
    installer verb reads (fold 13's read 170, N5, N6); and a refusal that named one of several triggers, which no row
    held, so a host was refused again after it undid the one named, a remedy that fitted the installer alone, and a
    directory taken for a key file that is there (fold 14's reads 180, N4, and 181, N2); and a FIFO taken for one, which
    no row held, and a host remedy that told the operator to unset a variable the parenthesis did not name (fold 15's
    read 191, N4, N7); and a link to a directory or `/dev/null` taken for a key file that is there, which no row held,
    while the docstring read as if a link to a FIFO were held (fold 16's reads 200, N3 and 201, N6); and a socket or a
    link to a FIFO taken for one, which no row held, and a docstring that still read so (fold 17's reads 210, N1, N4 and
    211, N1)."""
    real_step_1 = ki.ensure_control_key                           # before the seams stub it
    if run == "fresh":
        _mock_fresh_seams(monkeypatch)
        argv = _FRESH + ["--no-break-glass"]
    else:
        sops_path = _existing_instance(fresh_repo, monkeypatch)
        before = open(sops_path, encoding="utf-8").read()
        argv = []
    monkeypatch.setattr(ki, "INSTALLER_KEY_FILE", ki.AGE_KEY_FILE)     # the mount, here the fixture's key file
    marker, dispatcher = str(tmp_path / "installer-image"), str(tmp_path / "kontroll-installer")
    monkeypatch.setattr(ki, "INSTALLER_IMAGE_MARKERS", (marker, dispatcher))
    keys = os.path.dirname(ki.AGE_KEY_FILE)
    os.makedirs(keys, exist_ok=True)
    other = tmp_path / "other"
    other.mkdir()
    host = str(tmp_path / "home" / "op" / ".config" / "sops" / "age" / "kontroll.txt")   # as the host spells it
    monkeypatch.setenv("KONTROLL_IN_CONTAINER", "1")
    monkeypatch.setattr(ki, "ensure_control_key", _never("the control key"))
    monkeypatch.setattr(keygen, "generate_keypair", _never("the mint"))
    monkeypatch.chdir(str(other))
    os.symlink(str(other), os.path.join(keys, "out"))          # a link in the key directory that leads out
    moved = str(tmp_path / "h" / ".config" / "sops" / "age" / "keys.txt")

    def _refused(err, key_file, keys, there=False, why="KONTROLL_IN_CONTAINER is set"):
        assert ("%s is a key file no installer verb reads, and a run that writes the instance would key the instance "
                "to it." % key_file if there else "there is no key file at %s." % key_file) in err
        assert "In the installer container (%s), kontroll-init refuses" % why in err
        assert "refuses a control key file anywhere but keys.txt in %s," % keys in err
        assert "or with --key-file %s:" % ki.INSTALLER_KEY_FILE in err and "HOME=/root" in err and "<name>" not in err
        assert "Run with no --key-file and HOME=/root" in err
        assert "the one file every installer verb reads" in err and "is one no installer verb reads" in err
        assert "lost when the container exits, or left in the checkout or another mount" in err
        assert "for a --fresh run, through fresh-init, which hands the key back" in err
        assert ("unless the file is there and the run only reads it (an existing instance's "
                "--break-glass-recipient)") in err
        assert "move a control key kept under another name there first. Nothing was written" in err
        assert ("Nothing was written. If this is not the installer container, undo what the parenthesis names: unset "
                "KONTROLL_IN_CONTAINER if it names it, and move away each file it names.") in err
    hows = (("true", "true", []), ("0", "0", []), ("marker", None, [marker]), ("dispatcher", None, [dispatcher]),
            ("moved-home", None, [marker]), ("1+marker", "1", [marker]), ("both", None, [marker, dispatcher]),
            ("1+both", "1", [marker, dispatcher]))     # any value; `sudo` or `env -i`; another HOME; several at once
    for how, value, files in hows:
        if value:
            monkeypatch.setenv("KONTROLL_IN_CONTAINER", value)
        else:
            monkeypatch.delenv("KONTROLL_IN_CONTAINER", raising=False)
        for m in files:
            open(m, "w").close()
        flags = [] if how == "moved-home" else ["--key-file", host]
        if how == "moved-home":
            monkeypatch.setattr(ki, "AGE_KEY_FILE", moved)       # no --key-file: the default under HOME=/tmp/h
            os.makedirs(os.path.dirname(moved))                  # whose key directory is there
        capsys.readouterr()
        assert ki.main(argv + flags) == 1, how
        why = "; ".join((["KONTROLL_IN_CONTAINER is set"] if value else []) + ["%s is there" % m for m in files])
        _refused(capsys.readouterr().err, moved if how == "moved-home" else host, keys, why=why)
        assert not os.path.exists(moved) and not os.path.exists(str(tmp_path / "home")), how
        if run == "fresh":
            assert not os.path.exists(os.path.join(fresh_repo, "instance"))
        else:
            assert open(sops_path, encoding="utf-8").read() == before
        monkeypatch.setattr(ki, "AGE_KEY_FILE", ki.INSTALLER_KEY_FILE)
        for m in (marker, dispatcher):
            if os.path.exists(m):
                os.remove(m)
    monkeypatch.setenv("KONTROLL_IN_CONTAINER", "1")
    for key_file in (host, os.path.join(os.path.dirname(host), "keys.txt"), str(other / "keys.txt"), "keys.txt",
                     os.path.join(keys, "kontroll.txt"), os.path.join(keys, "out", os.pardir, "keys.txt")):
        capsys.readouterr()
        assert ki.main(argv + ["--key-file", key_file]) == 1, key_file
        err = capsys.readouterr().err
        _refused(err, key_file, keys)
        if run == "fresh":                                       # the break-glass refusal and mint it comes before
            assert ki.main(_FRESH + ["--key-file", key_file]) == 1, key_file
            _refused(capsys.readouterr().err, key_file, keys)
            monkeypatch.setattr(ki, "_stdout_is_terminal", lambda: True)
            assert ki.main(_FRESH + ["--mint-break-glass", "--key-file", key_file]) == 1, key_file
        assert not os.path.exists(str(tmp_path / "home")) and not os.listdir(str(other)), key_file
        if run == "fresh":
            assert not os.path.exists(os.path.join(fresh_repo, "instance"))
        else:
            assert open(sops_path, encoding="utf-8").read() == before
    for there in (str(tmp_path / "elsewhere" / "kontroll.txt"), os.path.join(keys, "kontroll.txt"),
                  str(tmp_path / "away" / "keys.txt")):
        _write_key(there, MINTED)                       # there already, a name or a directory no installer verb reads
        capsys.readouterr()
        assert ki.main(argv + ["--key-file", there]) == 1, there
        _refused(capsys.readouterr().err, there, keys, there=True)
        if run == "fresh":
            assert ki.main(_FRESH + ["--break-glass-recipient", BG, "--key-file", there]) == 1, there   # keys to it
            _refused(capsys.readouterr().err, there, keys, there=True)
            assert not os.path.exists(os.path.join(fresh_repo, "instance"))
        else:
            assert open(sops_path, encoding="utf-8").read() == before
            monkeypatch.setattr(ki, "ensure_control_key", lambda kf: (ki._read_public(kf), False))   # step 1 reads
            assert ki.main(["--break-glass-recipient", BG, "--key-file", there]) == 0, there   # only read
            monkeypatch.setattr(ki, "ensure_control_key", _never("the control key"))
            assert BG in open(sops_path, encoding="utf-8").read(), there
            with open(sops_path, "w", encoding="utf-8") as fh:
                fh.write(before)
        os.remove(there)
    dangling = os.path.join(keys, "gone.txt")
    os.symlink(str(tmp_path / "nowhere" / "k.txt"), dangling)    # a link to no file: no key there to read
    fifo = os.path.join(keys, "pipe.txt")
    os.mkfifo(fifo)                                              # nor is a FIFO
    to_fifo = os.path.join(keys, "to-pipe.txt")
    os.symlink(fifo, to_fifo)                                    # nor a link to one
    sock = os.path.join(keys, "sock.txt")
    os.mknod(sock, 0o600 | stat.S_IFSOCK)                        # nor a socket
    to_dir = os.path.join(keys, "out")                           # the link to a directory made above
    for nokey in (dangling, keys, fifo, to_fifo, sock, to_dir, os.devnull):   # a directory, a link to one, /dev/null
        for args in (argv, (_FRESH if run == "fresh" else []) + ["--break-glass-recipient", BG]):
            capsys.readouterr()
            assert ki.main(args + ["--key-file", nokey]) == 1, (nokey, args)
            _refused(capsys.readouterr().err, nokey, keys)
            assert not os.path.exists(str(tmp_path / "nowhere")), args
            if run == "fresh":
                assert not os.path.exists(os.path.join(fresh_repo, "instance"))
            else:
                assert open(sops_path, encoding="utf-8").read() == before
    for p in (dangling, fifo, to_fifo, sock):
        os.remove(p)
    made = []
    monkeypatch.setattr(ki, "ensure_control_key", lambda kf: made.append(kf) or (MINTED, True))
    present = str(other / "present.txt")

    def _goes_on(key_file):
        if run == "fresh":
            shutil.rmtree(os.path.join(fresh_repo, "instance"), ignore_errors=True)
        del made[:]
        assert ki.main(argv + ["--key-file", key_file]) == 0, (key_file, capsys.readouterr().err)
        assert made == [key_file]
    monkeypatch.chdir(keys)
    _goes_on("keys.txt")                                         # a bare name, read from the key directory
    _goes_on(os.path.join(keys, os.pardir, os.path.basename(keys), "keys.txt"))
    os.symlink(keys, str(tmp_path / "keys-link"))
    _goes_on(str(tmp_path / "keys-link" / "keys.txt"))           # the key directory through a link
    if not os.path.exists(ki.INSTALLER_KEY_FILE):
        _write_key(ki.INSTALLER_KEY_FILE, MINTED)
    os.symlink(ki.INSTALLER_KEY_FILE, str(tmp_path / "mounted.txt"))
    _goes_on(str(tmp_path / "mounted.txt"))                      # keys.txt itself, under another name
    monkeypatch.delenv("KONTROLL_IN_CONTAINER")
    _goes_on(host)                                               # the operator's machine: step 1 makes it, as before
    monkeypatch.chdir(str(other))
    monkeypatch.setattr(ki, "ensure_control_key", real_step_1)
    orig_run = ki.subprocess.run

    def _keygen(cmd, **kw):                                      # age-keygen -o, through the working directory
        if cmd[:2] != ["age-keygen", "-o"]:
            return orig_run(cmd, **kw)
        with open(cmd[2], "w", encoding="utf-8") as fh:
            fh.write("# public key: %s\nAGE-SECRET-KEY-1FAKEZZ\n" % MINTED)
        return type("P", (), {"returncode": 0})()
    monkeypatch.setattr(ki.subprocess, "run", _keygen)
    if run == "fresh":
        shutil.rmtree(os.path.join(fresh_repo, "instance"), ignore_errors=True)
    assert ki.main(argv + ["--key-file", "present.txt"]) == 0, capsys.readouterr().err   # step 1 with a bare name
    assert os.path.isfile(present)


def test_the_container_guard_reads_what_the_installer_sets():
    """The guard's constants are the installer's: docker/services/installer.yaml's `init` service sets
    KONTROLL_IN_CONTAINER to "1", its SOPS_AGE_KEY_FILE is INSTALLER_KEY_FILE, and it mounts the operator's
    ~/.config/sops/age at that file's directory; and docker/installer/Dockerfile's last stage, the one the image keeps
    (from its last `FROM`, in any case or indentation), makes the first of INSTALLER_IMAGE_MARKERS on one RUN line, the
    only line there that names the marker's directory, and copies the dispatcher to the second. Guards the guard going
    quiet in the container it exists for: a service that stopped setting the variable, a key file or mount moved without
    kontroll-init, or an image without either marker would each let a control key be made where the container discards
    it, with every guard row still passing, as they set their own values (T11 fold 11's read 151, N1, and read 150, N2);
    and a marker made in a stage the image does not keep, or removed by a later line, which this test passed (fold 12's
    read 161, N2), a later stage begun by a lower-case `from` or a removal of the marker's directory among them (fold
    13's read 171, N4), and one begun by an indented `FROM` or a tab after it (fold 14's read 181, N4)."""
    tool = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(tool, "docker", "services", "installer.yaml"), encoding="utf-8") as fh:
        init = yaml.safe_load(fh)["services"]["init"]
    assert init["environment"]["KONTROLL_IN_CONTAINER"] == "1"
    assert init["environment"]["SOPS_AGE_KEY_FILE"] == ki.INSTALLER_KEY_FILE
    mounts = [v.split(":")[1] for v in init["volumes"] if v.split(":")[0].endswith("/.config/sops/age")]
    assert mounts == [os.path.dirname(ki.INSTALLER_KEY_FILE)], init["volumes"]
    with open(os.path.join(tool, "docker", "installer", "Dockerfile"), encoding="utf-8") as fh:
        lines = [line.rstrip() for line in fh if not line.lstrip().startswith("#")]
    stage = lines[max(i for i, line in enumerate(lines)                                  # the image kept
                      if (line.split(None, 1) or [""])[0].upper() == "FROM"):]
    marker, dispatcher = ki.INSTALLER_IMAGE_MARKERS
    named = [line for line in stage if os.path.dirname(marker) in line]                  # its directory's too
    assert len(named) == 1 and named[0].startswith("RUN ") and named[0].endswith(": > %s" % marker), named
    assert [line for line in stage if line.startswith("COPY ") and line.endswith(" " + dispatcher)], stage


@_POSIX_ONLY
@pytest.mark.parametrize("shape", ["bare", "relative-sub", "missing-dotdot-abs", "missing-dotdot-rel", "link-dotdot"])
def test_step_one_makes_the_key_where_the_kernel_opens_its_path(tmp_path, monkeypatch, shape):
    """Step 1 (`ensure_control_key`), run for real with age-keygen stubbed by a writer that opens the path as age-keygen
    does, makes the key there on a bare name, a relative path, and paths with `..` after a directory that is missing or
    a link that leads out, with the file 0600 and the key's own directory, where step 1 makes it, 0700, and makes no
    directory at a lexical place the kernel does not open. Guards step 1 making the key's directory where the kernel
    will not look: fold 12's `os.path.abspath` folded `miss/..` and `out/..` lexically, so age-keygen failed after the
    scaffold and left a stray directory, and fold 11's `os.path.dirname` crashed on a bare name (T11 fold 12's read 160,
    N2; fold 11's read 150, N3); and a step 1 that drops the file's 0600, which every run's "private: … (0600)" line
    claims, or the directory's 0700, which `ensure_control_key`'s docstring claims; no test read either (fold 13's read
    171, N7)."""
    def _keygen(cmd, **kw):
        assert cmd[:2] == ["age-keygen", "-o"], cmd
        with open(cmd[2], "w", encoding="utf-8") as fh:          # opened as age-keygen opens it
            fh.write("# public key: %s\nAGE-SECRET-KEY-1FAKEZZ\n" % MINTED)
        return type("P", (), {"returncode": 0})()
    monkeypatch.setattr(ki.subprocess, "run", _keygen)
    work = tmp_path / "work"
    work.mkdir()
    other = tmp_path / "other" / "d"
    other.mkdir(parents=True)
    monkeypatch.chdir(str(work))
    kf = {"bare": "k.txt", "relative-sub": os.path.join("sub", "k.txt"),
          "missing-dotdot-abs": str(tmp_path / "miss" / ".." / "k3" / "k.txt"),
          "missing-dotdot-rel": os.path.join("nothere", "..", "k2", "k.txt"),
          "link-dotdot": os.path.join("out", "..", "k4", "k.txt")}[shape]
    if shape == "link-dotdot":
        os.symlink(str(other), "out")
    assert ki.ensure_control_key(kf) == (MINTED, True) and os.path.isfile(kf), kf
    assert os.stat(kf).st_mode & 0o777 == 0o600, kf
    if shape != "bare":
        assert os.stat(os.path.dirname(kf)).st_mode & 0o777 == 0o700, kf
    if shape == "link-dotdot":
        assert not os.path.exists(str(work / "k4")), "a stray directory at the lexical place"


@_POSIX_ONLY
def test_the_key_file_flag_names_the_file_every_check_reads(fresh_repo, monkeypatch, capsys, tmp_path):
    """`--key-file` names the file that the `# public key:` read, the presence check, age's own read and step 1 each
    see: a misread file there is refused with age asked about that file and the refusal naming it, a link to nothing
    there is refused before the scaffold, and a good key there is the key the instance is written to. Guards a check
    wired to the default path, which passed every other test (none passed --key-file): it refuses every good
    `--key-file` run, or lets a link to nothing at the named path scaffold instance/ and die in a traceback (T11 fold
    6's read 100, S1); a refusal telling the operator to fix the default path's file, not the one they named (fold 7's
    read 111, N2; its first naming held by fold 8's read 121, N2); and step 1 minting a key at the default path and
    writing the instance to it, while the run prints the named path as the key to keep (read 111, S1)."""
    _mock_fresh_seams(monkeypatch)
    elsewhere = str(tmp_path / "elsewhere" / "keys.txt")
    _write_key(elsewhere, MINTED)
    seen = []
    monkeypatch.setattr(ki, "_age_reads_public", lambda key_file: seen.append(key_file) or OTHER)
    assert ki.main(_FRESH + ["--no-break-glass", "--key-file", elsewhere]) == 1
    err = capsys.readouterr().err
    assert seen == [elsewhere] and "is not the public half" in err and "age-keygen -y %s" % elsewhere in err
    assert "line in %s is not" % elsewhere in err          # the refusal names the file, not the default path
    link = tmp_path / "linked" / "keys.txt"
    link.parent.mkdir()
    os.symlink(str(link.parent / "gone"), str(link))
    monkeypatch.setattr(ki, "ensure_control_key", _never("the control key"))
    assert ki.main(_FRESH + ["--no-break-glass", "--key-file", str(link)]) == 1
    err = capsys.readouterr().err
    assert "could not read a `# public key:` line from %s" % link in err
    assert not os.path.exists(os.path.join(fresh_repo, "instance"))
    asked = []                                   # a good key there: the instance is written to that key
    monkeypatch.setattr(ki, "ensure_control_key", lambda kf: asked.append(kf) or (OTHER, False))
    monkeypatch.setattr(ki, "_age_reads_public", lambda key_file: OTHER)
    _write_key(elsewhere, OTHER)
    assert ki.main(_FRESH + ["--no-break-glass", "--key-file", elsewhere]) == 0
    assert asked == [elsewhere] and all(r == {OTHER} for r in _rules().values())


@pytest.mark.skipif(os.name == "nt", reason="runs a stand-in age-keygen from PATH")
def test_the_age_read_runs_age_keygen_on_the_key_file(tmp_path, monkeypatch, capfd):
    """`_age_reads_public` runs `age-keygen -y <key file>` from PATH, the file named as kontroll-init reads it (a
    relative path led by `./`, an absolute one as given, with no working directory looked up, so a removed one does not
    refuse an absolute path), decodes its output as UTF-8 whatever the locale, bounds it at the 60 s the refusal names,
    and answers the first public half it prints; "" when age-keygen fails (it cannot read a key there) whatever it
    printed, prints nothing, prints what is not UTF-8, cannot be run (a file that is no program, or one on PATH without
    an execute bit) or runs past its bound; and None when no age-keygen is on PATH. An executable one later on PATH is
    preferred to one without the bit. What age-keygen writes to stderr is captured, not shown. Guards the seam the other
    tests patch: an argv that asks age-keygen something else, or hands it a file named `-k.txt` as a flag; a failure
    read as agreement, or a missing tool read as a key age cannot read, which would refuse every run on a machine
    without age; an age-keygen that cannot be run read as no age-keygen, which would skip the check (T11 fold 6's read
    100, N1), as it did for a file on PATH without an execute bit, which `shutil.which` does not count (fold 9's read
    130, S1); a run with no bound, an output that is not UTF-8 ending the run in a traceback, and
    age-keygen's stderr reaching the terminal (read 90, N4); the working directory joined to the path, which refused a
    good absolute file when that directory had been removed (fold 8's read 120, N1); and decoding in the locale's
    encoding, under which a runner whose locale is not UTF-8 decodes the row that prints byte 0xFF as text and answers
    it, where it should answer "" (read 121, N6; fold 9's read 131, N7)."""
    bin_dir, key_file, argv_log = tmp_path / "bin", tmp_path / "keys.txt", tmp_path / "argv"
    bin_dir.mkdir()
    key_file.write_text("# public key: age1first\nAGE-SECRET-KEY-1FAKE\n", encoding="utf-8")
    stub = bin_dir / "age-keygen"
    monkeypatch.setenv("PATH", str(bin_dir))
    assert ki._age_reads_public(str(key_file)) is None
    for body, want in (('printf "age1first\\nage1second\\n"', "age1first"), ("echo age1stale; exit 1", ""),
                       ("true", ""), ("printf 'age1\\377\\n'", ""),
                       ('echo "age-keygen: a note" >&2; echo age1first', "age1first")):
        stub.write_text('#!/bin/sh\necho "$@" > "%s"\n%s\n' % (argv_log, body), encoding="utf-8")
        stub.chmod(0o755)
        capfd.readouterr()
        assert ki._age_reads_public(str(key_file)) == want, body
        assert argv_log.read_text(encoding="utf-8").split() == ["-y", str(key_file)]
        assert capfd.readouterr().err == "", body
    monkeypatch.chdir(tmp_path)
    shutil.copy(str(key_file), "-k.txt")
    assert ki._age_reads_public("-k.txt") == "age1first"
    assert argv_log.read_text(encoding="utf-8").split() == ["-y", os.path.join(os.curdir, "-k.txt")]
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()                                   # the working directory removed under the run
    assert ki._age_reads_public(str(key_file)) == "age1first", "an absolute path needs no working directory"
    monkeypatch.chdir(tmp_path)
    stub.write_bytes(b"\x00not a program\n")
    assert ki._age_reads_public(str(key_file)) == ""
    stub.write_text("#!/bin/sh\necho age1first\n", encoding="utf-8")
    stub.chmod(0o644)                              # on PATH, no execute bit: a shell says "Permission denied"
    assert ki._age_reads_public(str(key_file)) == "", "one that cannot be executed gives no answer, never None"
    later = tmp_path / "later"
    later.mkdir()
    (later / "age-keygen").write_text("#!/bin/sh\necho age1later\n", encoding="utf-8")
    (later / "age-keygen").chmod(0o755)
    monkeypatch.setenv("PATH", "%s%s%s" % (bin_dir, os.pathsep, later))
    assert ki._age_reads_public(str(key_file)) == "age1later", "an executable one later on PATH is preferred"

    def _past_its_bound(argv, **kw):
        assert kw.get("timeout") == 60 and kw.get("encoding") == "utf-8", kw
        raise subprocess.TimeoutExpired(argv, kw["timeout"])
    monkeypatch.setattr(ki.subprocess, "run", _past_its_bound)
    assert ki._age_reads_public(str(key_file)) == ""


@pytest.mark.skipif(os.name == "nt", reason="runs a stand-in age-keygen from PATH")
def test_the_age_read_names_the_file_the_kernel_opens(tmp_path, monkeypatch):
    """`_age_reads_public` hands age-keygen a path that opens the file kontroll-init itself reads: a `..` after a
    symlinked directory is resolved by the kernel, not dropped lexically, and a file named `-k.txt` is still not a flag.
    Guards os.path.abspath, which turns `l/../keys.txt` into `keys.txt` in the directory that holds the link (fold 8's
    read 121, N8), so age is asked about another file: a good key file refused, or a misread one passed where that other
    file holds the line's key (T11 fold 7's read 110, S1)."""
    real = tmp_path / "a" / "b"
    (real / "sub").mkdir(parents=True)
    (real / "keys.txt").write_text("# public key: age1first\nAGE-SECRET-KEY-1FAKE\n", encoding="utf-8")
    os.symlink(str(real / "sub"), str(tmp_path / "l"))
    bin_dir, argv_log = tmp_path / "bin", tmp_path / "argv"
    bin_dir.mkdir()
    stub = bin_dir / "age-keygen"
    stub.write_text('#!/bin/sh\necho "$2" > "%s"\necho age1first\n' % argv_log, encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.chdir(tmp_path)
    for key_file in ("l/../keys.txt", str(tmp_path / "l" / ".." / "keys.txt"), "-k.txt"):
        if key_file == "-k.txt":
            shutil.copy(str(real / "keys.txt"), "-k.txt")
        assert ki._age_reads_public(key_file) == "age1first"
        handed = argv_log.read_text(encoding="utf-8").strip()
        assert not handed.startswith("-") and os.path.exists(handed) and os.path.samefile(handed, key_file), handed


def test_an_existing_instance_break_glass_run_mints_and_changes_no_secret(fresh_repo, monkeypatch, capsys):
    """Without --fresh, a `--break-glass-recipient` run on an instance whose bootstrap domains lack fields exits 0
    after adding the recipient and never reaches the bootstrap-secrets step: no plan is built, nothing is written,
    nothing secret is printed, and it names the runbook's next steps; a second run, the recipient already present,
    stops there too. Guards the existing-instance path minting, printing and re-encrypting an absent bootstrap
    secret — an older instance's Grafana admin password, a value the running service does not have — that
    docs/break-glass.md says this procedure does not touch, and the stop holding only on the run that adds the
    recipient."""
    _existing_instance(fresh_repo, monkeypatch)
    monkeypatch.setattr(secrets_service, "_existing_keys", lambda d: set())       # every bootstrap field absent
    monkeypatch.setattr(secrets_service, "build_secret_plan", _never("the bootstrap secrets"))
    monkeypatch.setattr(secrets_service, "apply_secret_plan", _never("a secret write"))
    capsys.readouterr()
    assert ki.main(["--break-glass-recipient", BG]) == 0
    out = capsys.readouterr().out
    assert "no secret was minted or changed" in out and "docs/break-glass.md" in out
    assert "SAVE" not in out and "bootstrap secrets" not in out
    assert all(s == {MINTED, BG} for s in _rules().values())
    assert ki.main(["--break-glass-recipient", BG]) == 0
    out = capsys.readouterr().out
    assert "already present" in out and "no secret was minted or changed" in out and "SAVE" not in out


def test_the_terminal_check_reads_standard_output(monkeypatch):
    """`_stdout_is_terminal` follows standard output alone: True when only standard output is a terminal, False
    when only standard input and standard error are, and False when there is no standard output or it is closed.
    Guards the seam reading the wrong stream — with it, `--mint-break-glass > file` run with standard error on a
    terminal would print the private key into the file — and a missing or closed standard output counted as a
    terminal."""
    class _Stream:
        def __init__(self, tty):
            self._tty = tty

        def isatty(self):
            return self._tty
    for out, other, want in ((True, False, True), (False, True, False)):
        monkeypatch.setattr(sys, "stdout", _Stream(out))
        monkeypatch.setattr(sys, "stderr", _Stream(other))
        monkeypatch.setattr(sys, "stdin", _Stream(other))
        assert ki._stdout_is_terminal() is want
    monkeypatch.setattr(sys, "stdout", None)
    assert ki._stdout_is_terminal() is False

    class _Closed:
        def isatty(self):
            raise ValueError("I/O operation on closed file")
    monkeypatch.setattr(sys, "stdout", _Closed())
    assert ki._stdout_is_terminal() is False


def test_mint_refuses_when_standard_output_is_not_a_terminal(fresh_repo, monkeypatch, capsys):
    """`--mint-break-glass` whose standard output is not a terminal (a pipe, a file, a captured CI or dogfood run)
    exits 1 before anything is minted or written, and names `--break-glass-recipient` as the unattended form; the
    real terminal check reads pytest's capture as not a terminal. Guards a show-once private key landing in a log
    nobody chose to keep, and — were the show suppressed instead — a recipient whose private half nobody was shown."""
    assert ki._stdout_is_terminal() is False
    _mock_fresh_seams(monkeypatch)
    monkeypatch.setattr(ki, "_stdout_is_terminal", lambda: False)
    monkeypatch.setattr(keygen, "generate_keypair", _never("the mint"))
    assert ki.main(_FRESH + ["--mint-break-glass"]) == 1
    err = capsys.readouterr().err
    assert "not a terminal" in err and "--break-glass-recipient" in err
    assert not os.path.exists(os.path.join(fresh_repo, "instance"))


@pytest.mark.parametrize("flag", ["--mint-break-glass", "--no-break-glass"])
def test_the_mint_and_the_opt_out_are_choices_for_fresh_only(fresh_repo, monkeypatch, capsys, flag):
    """`--mint-break-glass` and `--no-break-glass` without --fresh exit 1 before any write and point at
    `--break-glass-recipient` and docs/break-glass.md; two of the three choices together are an argparse error.
    Guards an existing instance minting a key on the node outside the one flow that shows it at birth, and a
    `--no-break-glass` on an existing instance reading as though it removed a recipient."""
    _mock_fresh_seams(monkeypatch)
    monkeypatch.setattr(ki, "ensure_control_key", _never("the control key"))
    assert ki.main([flag]) == 1
    err = capsys.readouterr().err
    assert "choices for --fresh" in err and "--break-glass-recipient" in err and "docs/break-glass.md" in err
    with pytest.raises(SystemExit) as exc:
        ki.main(_FRESH + [flag, "--break-glass-recipient", BG])
    assert exc.value.code == 2


def test_a_failed_mint_writes_nothing(fresh_repo, monkeypatch, capsys):
    """When `--mint-break-glass` cannot mint (age-keygen absent or failing), kontroll-init exits 1 with the reason,
    before the control key or instance/ is made. Guards the mint's failure surfacing as a traceback halfway through
    — or, were it caught later, an instance created without the recipient its operator chose."""
    _mock_fresh_seams(monkeypatch)
    monkeypatch.setattr(ki, "_stdout_is_terminal", lambda: True)
    monkeypatch.setattr(keygen, "generate_keypair", lambda: None)
    monkeypatch.setattr(ki, "ensure_control_key", _never("the control key"))
    assert ki.main(_FRESH + ["--mint-break-glass"]) == 1
    assert "could not mint the break-glass key" in capsys.readouterr().err
    assert not os.path.exists(os.path.join(fresh_repo, "instance"))


def test_a_fresh_run_refused_for_its_trust_mode_or_a_configured_overlay_mints_no_key(fresh_repo, monkeypatch, capsys):
    """`--fresh --mint-break-glass` without `--trust-mode`, or on an overlay that already names real recipients, exits
    1 before the break-glass key is minted. Guards the mint running ahead of the --fresh checks, so that "refuses
    before minting anything" held for the break-glass refusals only, and a key was minted and discarded for a run
    that could never use it."""
    _mock_fresh_seams(monkeypatch)
    monkeypatch.setattr(ki, "_stdout_is_terminal", lambda: True)
    monkeypatch.setattr(keygen, "generate_keypair", _never("the mint"))
    no_trust = [a for a in _FRESH if a not in ("--trust-mode", "separated")]
    assert ki.main(no_trust + ["--mint-break-glass"]) == 1
    assert "--fresh requires --trust-mode" in capsys.readouterr().err
    _existing_instance(fresh_repo, monkeypatch)
    capsys.readouterr()
    assert ki.main(_FRESH + ["--mint-break-glass"]) == 1
    assert "already names real recipients" in capsys.readouterr().err


@pytest.mark.parametrize("choice", [["--break-glass-recipient", BG], ["--mint-break-glass"]], ids=["supplied", "minted"])
def test_a_failed_recipient_write_stops_before_any_secret(fresh_repo, monkeypatch, capsys, choice):
    """When the break-glass recipient cannot be added to .sops.yaml, kontroll-init exits 1 with the writer's reason
    and writes no secret; a minted key is then never shown (its public half is in no rule, so its private half opens
    nothing). Guards the first secrets being written to the control key alone while the operator believes there are
    two recipients — the state T11 exists to end — and a private key shown for a recipient that was never added."""
    _mock_fresh_seams(monkeypatch)
    monkeypatch.setattr(ki, "_stdout_is_terminal", lambda: True)
    monkeypatch.setattr(keygen, "generate_keypair", lambda: {"public": BG, "private": BG_PRIVATE})
    real = keygen.add_recipient
    monkeypatch.setattr(keygen, "add_recipient", lambda public, anchors: (
        {"changed": False, "error": "planted", "recipient_domains": []} if public == BG else real(public, anchors)))
    monkeypatch.setattr(secrets_service, "apply_secret_plan", _never("a secret write"))
    assert ki.main(_FRESH + choice) == 1
    out, err = capsys.readouterr()
    assert "could not add the break-glass recipient to .sops.yaml: planted" in err
    assert BG_PRIVATE not in out and BG_PRIVATE not in err
    assert all(s == {MINTED} for s in _rules().values())


def test_a_missing_break_glass_role_is_refused_before_any_write(fresh_repo, monkeypatch, capsys):
    """The groups a break-glass recipient joins come from key-roles/break-glass.yml, as the GUI dialog's do; with that
    role absent, or naming no groups, kontroll-init exits 1 before anything is written. Guards the CLI and the dialog
    disagreeing about which domains the key opens — a list of the CLI's own would drift from the role file."""
    _mock_fresh_seams(monkeypatch)
    monkeypatch.setattr(ki, "ensure_control_key", _never("the control key"))
    for role in (None, {"role": "break-glass", "recipient_groups": []}):
        monkeypatch.setattr(catalog, "key_role", lambda r, role=role: role)
        assert ki.main(_FRESH + ["--break-glass-recipient", BG]) == 1
        assert "key-roles/break-glass.yml" in capsys.readouterr().err
        assert not os.path.exists(os.path.join(fresh_repo, "instance"))
