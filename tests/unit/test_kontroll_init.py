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


@pytest.fixture
def fresh_repo(tmp_path, monkeypatch):
    """A throwaway ROOT holding ONLY the shipped instance.example/ skeleton and NO instance/ overlay — the state
    a brand-new node clones into. --fresh scaffolds instance/ from it; ROOT is repointed so the run never touches
    the real tree. Mirrors conftest.tmp_repo but for the fresh-install path."""
    shutil.copytree(os.path.join(paths.ROOT, "instance.example"), tmp_path / "instance.example")
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
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


def test_fresh_writes_sops_with_only_new_recipient(fresh_repo, monkeypatch, capsys):
    """SECURITY CONTRACT of --fresh: the scaffolded instance/.sops.yaml names ONLY the freshly-minted control
    key, in EVERY domain — the shipped placeholder is REPLACED, never appended to. This is the guard against the
    inherit-recipients footgun the blind-Joe audit found: if a fresh node kept another instance's recipients, a
    stranger could decrypt the new node's secrets (and the new node's own secrets could be encrypted to a key it
    doesn't hold). Also asserts the control private key is never printed."""
    _mock_fresh_seams(monkeypatch)
    rc = ki.main(["--fresh", "--trust-mode", "separated", "--mgmt-ip", "198.51.100.7", "--domain", "lab.test"])
    assert rc == 0
    sops_path = os.path.join(fresh_repo, "instance", ".sops.yaml")
    assert os.path.exists(sops_path)
    text = open(sops_path, encoding="utf-8").read()
    recips = keygen._resolve_recipients(text)
    assert recips and all(s == {MINTED} for s in recips.values())   # only the minted key, in every domain
    assert keygen.FRESH_PLACEHOLDER not in text                     # placeholder fully replaced (none left)
    assert "AGE-SECRET-KEY" not in capsys.readouterr().out


def test_fresh_scaffolds_skeleton_without_secrets_and_sets_instance_yml(fresh_repo, monkeypatch):
    """--fresh copies the instance.example/ skeleton into instance/ (instance.yml, fleet.yml, inventory, the
    homepage config) but NEVER the secrets/ subtree — a fresh node mints its own secrets, encrypted to its own
    key, and must not inherit ciphertext. The --mgmt-ip/--domain flags are written into instance/instance.yml so
    deploy-stack renders the right ${KONTROLL_MGMT_IP}/${KONTROLL_DOMAIN} (PR-2)."""
    _mock_fresh_seams(monkeypatch)
    assert ki.main(["--fresh", "--trust-mode", "separated", "--mgmt-ip", "198.51.100.7", "--domain", "lab.test"]) == 0
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
    assert ki.main(["--fresh", "--trust-mode", "separated", "--mgmt-ip", "198.51.100.7", "--domain", "lab.test"]) == 0
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
    assert ki.main(["--fresh", "--trust-mode", "separated", "--mgmt-ip", "198.51.100.7", "--domain", "lab.test"]) == 0
    hp = os.path.join(fresh_repo, "instance", "dashboards", "homepage", "services.yaml")
    before = open(hp, encoding="utf-8").read()
    assert ki._personalize_homepage("9.9.9.9", "other.test") == []   # no sentinels remain → no tokens replaced
    assert open(hp, encoding="utf-8").read() == before               # file untouched (byte-identical)


def test_fresh_refuses_configured_overlay(fresh_repo, monkeypatch, capsys):
    """--fresh REFUSES (returns 1) once the overlay names a real recipient with no placeholder — protecting an
    already-configured node (the source instance, or an accidental second --fresh) from being re-initialised as
    brand-new. The guard fires BEFORE any scaffold/personalize write, so a configured tree is never mutated."""
    _mock_fresh_seams(monkeypatch)
    assert ki.main(["--fresh", "--trust-mode", "separated"]) == 0                                 # first run seeds (placeholder -> minted)
    capsys.readouterr()
    rc2 = ki.main(["--fresh", "--trust-mode", "separated"])                                       # second run: real recipient, no placeholder
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
    assert ki.main(["--fresh", "--trust-mode", "separated", "--mgmt-ip", "1.2.3.4", "--domain", "x.test"]) == 0
    assert planned == []                                  # neither domain re-planned → no secret rotation


# --- F3 (trust_mode): the "never a silent default" rule enforced at BIRTH + the comment-tolerant write -------------
def test_fresh_requires_trust_mode(fresh_repo, monkeypatch, capsys):
    """F3 / the never-a-silent-default rule at the ONLY place a fresh overlay is born: `--fresh` WITHOUT
    `--trust-mode` exits 1 with a pointed message and writes NOTHING — kontroll never guesses whether you want a
    human-reviewed (separated) or an auto (solo) promote. Guards a fresh node silently defaulting its trust posture
    (the one setting that, unlike tls_mode, must fail LOUD not safe)."""
    _mock_fresh_seams(monkeypatch)
    rc = ki.main(["--fresh", "--mgmt-ip", "198.51.100.7", "--domain", "lab.test"])      # no --trust-mode
    assert rc == 1
    assert "requires --trust-mode" in capsys.readouterr().err
    assert not os.path.exists(os.path.join(fresh_repo, "instance", "instance.yml"))   # refused BEFORE any scaffold


def test_fresh_writes_chosen_trust_mode_uncommenting_the_template(fresh_repo, monkeypatch):
    """F3: `--fresh --trust-mode solo` writes an ACTIVE `trust_mode: solo` into instance.yml by UNCOMMENTING the
    template's `# trust_mode: separated` line (the comment-tolerant substitution), so the operator's chosen posture
    is the durable, deploy-read value and no stale commented default survives. Guards the chosen posture silently
    not landing (which would then hard-fail deploy-stack's assert) or the wrong value persisting."""
    _mock_fresh_seams(monkeypatch)
    assert ki.main(["--fresh", "--trust-mode", "solo", "--mgmt-ip", "198.51.100.7", "--domain", "lab.test"]) == 0
    iy = open(os.path.join(fresh_repo, "instance", "instance.yml"), encoding="utf-8").read()
    assert "trust_mode: solo" in iy                       # active, uncommented, the chosen value
    assert "trust_mode: separated" not in iy              # the commented template default is GONE (replaced)
    assert "# trust_mode:" not in iy                      # the assignment line is no longer commented
