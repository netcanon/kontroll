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
import sys

import pytest

from kontroll import catalog, paths
from kontroll.service import keygen
from kontroll.service import secrets as secrets_service

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


def test_main_idempotent_node_makes_no_change_and_never_prints_private(monkeypatch, capsys):
    """On a CONFIGURED node — key present, recipient present, dashboards already set — main returns 0, reports
    'no change', and NEVER prints an AGE-SECRET-KEY. The control private key lives only in the 0600 file; the
    CLI must not echo it."""
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
# A break-glass PUBLIC key made elsewhere: bech32 with a valid checksum, as kontroll-init requires, and one byte short
# of a real key, so the leak guard's real-length age rule never matches it.
BG = "age1vfex2cttvakxzumn946x2um594c82csqqqqqqqqqqqqqqqqqqqzjuph0"
OTHER = "age1othertestpubkey00000000000000000000000000000000000000"    # a key that is not this instance's control key
BG_PRIVATE = "AGE-SECRET-KEY-1FAKEBREAKGLASSPRIVATE"                     # a minted one's private half (a fake)
_FRESH = ["--fresh", "--trust-mode", "separated", "--mgmt-ip", "198.51.100.7", "--domain", "lab.test"]


@pytest.fixture
def fresh_repo(tmp_path, monkeypatch):
    """A throwaway ROOT holding ONLY the shipped instance.example/ skeleton and NO instance/ overlay — the state
    a brand-new node clones into. --fresh scaffolds instance/ from it; ROOT is repointed so the run never touches
    the real tree, and the default control-key path is repointed under it so a run never reads the operator's
    own key file. Mirrors conftest.tmp_repo but for the fresh-install path."""
    shutil.copytree(os.path.join(paths.ROOT, "instance.example"), tmp_path / "instance.example")
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    monkeypatch.setattr(ki, "AGE_KEY_FILE", str(tmp_path / "age" / "keys.txt"))
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
    standard error, and in no file under the scratch root (the overlay, the key directory); its public half is a
    recipient of every rule. Guards the private half reaching disk — where a backup, a snapshot or a commit would
    carry it — and its being printed twice (a log line besides the block) or not at all (a recipient nobody holds)."""
    _mock_fresh_seams(monkeypatch)
    monkeypatch.setattr(ki, "_stdout_is_terminal", lambda: True)
    monkeypatch.setattr(keygen, "generate_keypair", lambda: {"public": BG, "private": BG_PRIVATE})
    assert ki.main(_FRESH + ["--mint-break-glass"]) == 0
    out, err = capsys.readouterr()
    assert out.count(BG_PRIVATE) == 1 and BG_PRIVATE not in err
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
], ids=["private-key", "private-key-lower-padded", "too-short", "upper-case", "two-joined", "checksum-typo",
        "outside-the-alphabet"])
def test_a_malformed_or_private_recipient_is_refused_before_any_write(fresh_repo, monkeypatch, capsys,
                                                                     recipient, expect):
    """A `--break-glass-recipient` that is a PRIVATE key, or is not an age public key as age-keygen prints one, exits
    1 before anything is written, and the value is not echoed back; a private key in lower case or with a space
    before it is still named as one; a valid key with more after it (two keys joined by a comma) is refused whole; a
    value of the right shape whose bech32 checksum fails — one character changed, or one outside the bech32 alphabet
    — is refused as one (S2). Guards a typo creating a recipient nobody can decrypt with (sops refuses it only at the
    first encrypt, after the instance and its .sops.yaml are written, so the run fails half-way with an error that
    blames the age key and no re-run repairs it), a check that reads only a valid prefix, a private key pasted by
    mistake being printed to the terminal or its log a second time, and a lower-cased or padded one losing the advice
    that names it a private key."""
    _mock_fresh_seams(monkeypatch)
    monkeypatch.setattr(ki, "ensure_control_key", _never("the control key"))
    assert ki.main(_FRESH + ["--break-glass-recipient", recipient]) == 1
    err = capsys.readouterr().err
    assert expect in err and recipient not in err and "Nothing was written" in err
    assert not os.path.exists(os.path.join(fresh_repo, "instance"))


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
    such as a shell without the key directory's bind (MF-04)."""
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
    (None, True),
], ids=["key-group-list", "rule-level-string", "comma-in-a-list-item", "second-group", "no-age-rule-skipped",
        "one-rule-without-it", "rule-level-without-it", "no-file"])
def test_the_control_key_check_reads_every_recipient_form(fresh_repo, text, want):
    """`_control_everywhere` finds the control key in a rule whichever form names it — a key-group list, a
    comma-separated string on the rule itself, a list item holding a comma, any of several groups — skips a rule
    with no age recipient, and is False when one rule that names age recipients lacks it, in a key group or on the
    rule itself; with no .sops.yaml it defers to the write step, which refuses on its own. Guards the S3 refusal
    turning away a real control key written in a form the reader misses, or passing a key that one rule does not
    name — a reader blind to the rule's own `age:` saw no recipients there and passed any key (mutant F3-E3)."""
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
    it is, opens every secret, and the runbook's checks (a removed recipient) do not stop it (S3)."""
    sops_path = _existing_instance(fresh_repo, monkeypatch)
    before = open(sops_path, encoding="utf-8").read()
    _write_key(ki.AGE_KEY_FILE, OTHER)
    capsys.readouterr()
    assert ki.main(["--break-glass-recipient", BG]) == 1
    err = capsys.readouterr().err
    assert "is not a recipient of every rule" in err and ki.AGE_KEY_FILE in err and "Nothing was written" in err
    assert OTHER not in err
    assert open(sops_path, encoding="utf-8").read() == before


def test_an_existing_instance_break_glass_run_mints_and_changes_no_secret(fresh_repo, monkeypatch, capsys):
    """Without --fresh, a `--break-glass-recipient` run on an instance whose bootstrap domains lack fields exits 0
    after adding the recipient and never reaches the bootstrap-secrets step: no plan is built, nothing is written,
    nothing secret is printed, and it names the runbook's next steps. Guards the existing-instance path minting,
    printing and re-encrypting an absent bootstrap secret — an older instance's Grafana admin password, a value the
    running service does not have — that docs/break-glass.md says this procedure does not touch (S5)."""
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


def test_the_terminal_check_reads_standard_output(monkeypatch):
    """`_stdout_is_terminal` follows standard output alone: True when only standard output is a terminal, False
    when only standard input and standard error are, and False when there is no standard output or it is closed.
    Guards the MF-05 seam reading the wrong stream — with it, `--mint-break-glass > file` run with standard error on
    a terminal would print the private key into the file — and a missing or closed standard output counted as a
    terminal (N2)."""
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
    nobody chose to keep, and — were the show suppressed instead — a recipient whose private half nobody was shown
    (MF-05)."""
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
    that could never use it (N5)."""
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
