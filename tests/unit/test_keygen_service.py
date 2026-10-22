"""service/keygen.py — guided age key-generation (the second D headline, the most security-critical surface).
The crown-jewel properties are asserted as DATA: the generated PRIVATE key appears ONLY in the apply result's
`private_key` field (never in the .sops.yaml written, never returned by the plan); the .sops.yaml edit is
strictly ADDITIVE (no existing recipient is ever dropped) and idempotent; a scoped role lands ONLY in its
anchor group; and any keygen/verify failure writes NOTHING.

Why each guards a real failure: a leaked private key is the total compromise this feature must never cause; a
DROPPED recipient would make every secret unrecoverable (or a SMUGGLED one would over-expose them) — so the
parse-verify additive gate is the load-bearing safety net; a non-idempotent edit would churn the canonical on
every re-run. No real `age-keygen`/`sops` runs — the keygen shell-out seam is mocked.
"""
import os
import re
import shutil

import pytest

from kontroll import catalog, gitio, paths
from kontroll.service import keygen

pytestmark = pytest.mark.unit

# A syntactically-shaped age keypair (never a real key) the mocked keygen returns. The private key starts with
# the `1FAKE` sentinel so the gitleaks age-secret-key rule's allowlist exempts it (a real key never does).
FAKE = {"public": "age1pubdummyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxq",
        "private": "AGE-SECRET-KEY-1FAKETESTPRIVATEKEYZZZ"}


@pytest.fixture
def sops_repo(tmp_path, monkeypatch):
    """A throwaway repo root holding a COPY of the real /.sops.yaml, with paths.ROOT repointed at it and
    age-keygen mocked — so apply writes the throwaway file (never the real recipiency rules) using a fake
    keypair. The real key-roles/ descriptors still load (read-only registry input)."""
    shutil.copy(paths.resolve(".sops.yaml"), tmp_path / ".sops.yaml")
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    monkeypatch.setattr(gitio, "age_keygen", lambda: dict(FAKE))
    return tmp_path


def _written(sops_repo):
    return (sops_repo / ".sops.yaml").read_text(encoding="utf-8")


def test_registry_loads_the_key_roles():
    """load_key_roles() returns the dropped-in role descriptors sorted by order — the registry is the loader,
    never a hardcoded list (guards a god-map regression). break-glass(10) < semaphore(20) < control(30)."""
    assert catalog.registered_key_roles() == ["break-glass", "semaphore", "control"]


def test_plan_resolves_scope_and_carries_no_key_material():
    """build_keygen_plan resolves which domains the public key will join FROM /.sops.yaml: the scoped
    Semaphore key → operational domains ONLY; break-glass → every domain. The plan is pure descriptor data —
    no key exists yet, so nothing secret can appear."""
    sem = keygen.build_keygen_plan("semaphore")
    assert sem["error"] is None and sem["recipient_domains"] == ["network", "proxmox"]
    assert sem["placement"] == "path" and sem["private_key_path"].endswith("semaphore/age/keys.txt")
    bg = keygen.build_keygen_plan("break-glass")
    assert set(bg["recipient_domains"]) >= {"network", "proxmox", "dashboards", "semaphore"}
    assert bg["placement"] == "offline" and bg["high_blast"] is True
    assert "AGE-SECRET-KEY" not in repr(sem) and "AGE-SECRET-KEY" not in repr(bg)


def test_plan_unknown_role_is_clean_error():
    """An unregistered role is a clean no_role signal (the route maps it to 404), never a crash."""
    assert keygen.build_keygen_plan("not_a_role")["error"] == "no_role"


def test_add_recipient_is_additive_idempotent_and_anchor_scoped():
    """_add_recipient_to_anchor inserts a quoted recipient into the named anchor block, is idempotent on a
    re-add, and is anchor-preserving — a key added to ops_recipients reaches network/proxmox but NOT the base
    domains. Guards both the scoping (a Semaphore key must never gain the service domains) and file churn."""
    text = _read_real_sops()
    new, changed, err = keygen._add_recipient_to_anchor(text, "ops_recipients", FAKE["public"])
    assert changed and err is None
    again, changed2, err2 = keygen._add_recipient_to_anchor(new, "ops_recipients", FAKE["public"])
    assert changed2 is False and err2 is None                       # idempotent
    rec = keygen._resolve_recipients(new)
    net = next(v for k, v in rec.items() if "network" in k)
    dash = next(v for k, v in rec.items() if "dashboards" in k)
    assert FAKE["public"] in net and FAKE["public"] not in dash     # scoped to ops, not base


def test_verify_additive_catches_drop_smuggle_and_mistargeting():
    """_verify_additive — the crown-jewel safety net — REJECTS an edit that (a) dropped a prior recipient (→
    unrecoverable secrets), (b) added an unexpected one (→ over-exposure), or (c) added the key to a domain the
    role never named (over-grant), and ACCEPTS a clean add to EXACTLY the intended domains."""
    before, intended = {"d1": {"age1a", "age1b"}, "d2": {"age1a"}}, {"d1"}
    drop = {"d1": {"age1a", "age1new"}, "d2": {"age1a"}}                       # d1 lost age1b
    assert keygen._verify_additive(before, drop, "age1new", intended)[0] is False
    smuggle = {"d1": {"age1a", "age1b", "age1evil"}, "d2": {"age1a"}}          # an unexpected recipient
    assert keygen._verify_additive(before, smuggle, "age1new", intended)[0] is False
    over = {"d1": {"age1a", "age1b", "age1new"}, "d2": {"age1a", "age1new"}}   # leaked into d2 (not intended)
    assert keygen._verify_additive(before, over, "age1new", intended) == (False, "wrong_domains")
    clean = {"d1": {"age1a", "age1b", "age1new"}, "d2": {"age1a"}}             # exactly the intended domain
    assert keygen._verify_additive(before, clean, "age1new", intended) == (True, None)


def test_apply_shows_private_once_and_never_writes_it(sops_repo):
    """apply mints the keypair, writes ONLY the public recipient into /.sops.yaml, and returns the private key
    in the result (the one show-once surface). The crown jewel: the private key is in the result but appears
    NOWHERE in the file written — and the public key DID land in the scoped domains."""
    res = keygen.apply_keygen_plan("semaphore")
    assert res["error"] is None and res["changed"] is True
    assert res["private_key"] == FAKE["private"] and res["public_key"] == FAKE["public"]
    written = _written(sops_repo)
    assert FAKE["public"] in written                                 # the recipient landed
    assert "AGE-SECRET-KEY" not in written and FAKE["private"] not in written   # the private key NEVER does
    assert res["paths"] == [".sops.yaml"]   # flat fixture: no instance/ overlay, so overlay_rel is the identity


def test_apply_reports_the_overlay_resolved_commit_path(tmp_path, monkeypatch):
    """REGRESSION (live-caught 2026-06-29, the .50->.10 migration's first GUI keygen): when .sops.yaml lives in
    the instance/ OVERLAY (every real deploy), apply must report the OVERLAY-resolved repo-relative path
    `instance/.sops.yaml` — the path the route hands to `git add` — NOT the bare `.sops.yaml`. The old code
    hardcoded `[".sops.yaml"]`, so on a real box `git add .sops.yaml` from the clone root matched nothing (the
    changed file is instance/.sops.yaml) -> the keygen commit failed -> the break-glass/scoped recipient never
    staged (a silent crown-jewel-recovery gap). The flat fixture above never exercised the overlay branch
    (overlay_rel is file-existence based), so this sets up the overlay explicitly. The load-bearing invariant:
    os.path.join(ROOT, reported_path) == the file actually written (resolve), so the git-add can't miss it —
    exactly how the WORKING onboard path reports paths.overlay_rel(...)."""
    (tmp_path / "instance").mkdir()
    shutil.copy(paths.resolve(".sops.yaml"), tmp_path / "instance" / ".sops.yaml")   # the OVERLAY layout
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    monkeypatch.setattr(gitio, "age_keygen", lambda: dict(FAKE))
    res = keygen.apply_keygen_plan("semaphore")
    assert res["error"] is None and res["changed"] is True
    assert res["paths"] == ["instance/.sops.yaml"], "must report the overlay-resolved path git-add uses"
    assert os.path.join(paths.ROOT, res["paths"][0]) == paths.resolve(".sops.yaml"), \
        "the reported commit path must point at the file actually written (else `git add` misses it)"
    assert FAKE["public"] in (tmp_path / "instance" / ".sops.yaml").read_text(encoding="utf-8")


def test_apply_is_idempotent(sops_repo):
    """A second apply of a role whose public key is already a recipient re-writes NOTHING (changed=False) — no
    canonical churn / no redundant proposal. (Distinct fake keys per call would defeat this; the seam is fixed
    here, mirroring an operator re-running before promoting.)"""
    assert keygen.apply_keygen_plan("semaphore")["changed"] is True
    second = keygen.apply_keygen_plan("semaphore")
    assert second["changed"] is False and second["error"] is None


def test_apply_keygen_failure_writes_nothing(sops_repo, monkeypatch):
    """If age-keygen fails (returns None), apply is a clean keygen_failed error and the .sops.yaml is
    untouched — never a half-edited recipiency file."""
    before = _written(sops_repo)
    monkeypatch.setattr(gitio, "age_keygen", lambda: None)
    out = keygen.apply_keygen_plan("break-glass")
    assert out["error"] == "keygen_failed" and out["changed"] is False
    assert _written(sops_repo) == before


def test_apply_verify_failure_writes_nothing(sops_repo, monkeypatch):
    """If the additive parse-verify fails (a botched edit that would drop/smuggle a recipient), apply returns
    verify_failed and writes NOTHING — the load-bearing guard refuses to persist a dangerous recipiency file."""
    before = _written(sops_repo)
    # Force a non-additive new text: an editor that deletes the whole recipients section.
    monkeypatch.setattr(keygen, "_add_recipient_to_anchor",
                        lambda text, anchor, pub: ("creation_rules: []\n", True, None))
    out = keygen.apply_keygen_plan("break-glass")
    assert out["error"].startswith("verify_failed") and out["changed"] is False
    assert _written(sops_repo) == before


def test_apply_unknown_role_and_missing_sops_are_clean_errors(sops_repo, monkeypatch):
    """An unknown role → no_role; a missing /.sops.yaml → no_sops_config. Both clean, both write nothing."""
    assert keygen.apply_keygen_plan("not_a_role")["error"] == "no_role"
    (sops_repo / ".sops.yaml").unlink()
    assert keygen.apply_keygen_plan("break-glass")["error"] == "no_sops_config"


def test_age_keygen_parses_and_prints_nothing(monkeypatch, capsys):
    """gitio.age_keygen parses age-keygen's stdout into {public, private} AND prints nothing — the narrated
    `$ cmd` echo every other gitio shell-out emits is deliberately ABSENT here, because stdout carries the
    private key (a `$ age-keygen` echo would be fine, but any output risks surfacing the key, so it stays
    silent). A non-zero rc → None."""
    from types import SimpleNamespace
    out = ("# created: 2026-06-15T00:00:00Z\n"
           "# public key: age1realpublishedkeyxxxxxxxxxxxxxxxxxxxxxxxxxxxxq\n"
           "AGE-SECRET-KEY-1FAKEABCDEF0123456789\n")     # 1FAKE sentinel → gitleaks-allowlisted
    monkeypatch.setattr("subprocess.run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=out))
    pair = gitio.age_keygen()
    assert pair == {"public": "age1realpublishedkeyxxxxxxxxxxxxxxxxxxxxxxxxxxxxq",
                    "private": "AGE-SECRET-KEY-1FAKEABCDEF0123456789"}
    captured = capsys.readouterr()
    assert "AGE-SECRET-KEY" not in captured.out and "AGE-SECRET-KEY" not in captured.err

    monkeypatch.setattr("subprocess.run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    assert gitio.age_keygen() is None


def _read_real_sops():
    return open(paths.resolve(".sops.yaml"), encoding="utf-8").read()


# --- F4 (dogfood): a fresh-seeded .sops.yaml must stay additive-writable -----------------------------------

@pytest.fixture
def fresh_example_sops(tmp_path, monkeypatch):
    """ROOT holding a copy of the SHIPPED instance.example/.sops.yaml (placeholder recipient) — the exact
    template a brand-new node seeds from (not the dev's configured one)."""
    shutil.copy(os.path.join(paths.ROOT, "instance.example", ".sops.yaml"), tmp_path / ".sops.yaml")
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    return tmp_path


def test_seed_fresh_leaves_clean_lines_and_stays_additive(fresh_example_sops):
    """F4 (live dogfood): seed_fresh_sops must REPLACE the placeholder AND strip the template's trailing
    '# REPLACE' marker, so the seeded recipient line is clean. Otherwise the EOL-anchored _RECIPIENT_RE misses
    the commented line and the later additive add_recipient (the GUI break-glass key-add, SETUP §5.2) fails
    `no_recipients` on a brand-new overlay — which is exactly what the blind install hit."""
    newpub = "age1freshfreshfreshfreshfreshfreshfreshfreshfreshfresh00"
    s = keygen.seed_fresh_sops(newpub)
    assert s["error"] is None and s["changed"]
    text = (fresh_example_sops / ".sops.yaml").read_text(encoding="utf-8")
    assert keygen.FRESH_PLACEHOLDER not in text                          # placeholder fully replaced
    assert not re.search(r'age1[0-9a-z]+"?\s*#\s*REPLACE', text)         # no stale marker on a recipient line
    assert set(re.findall(r"age1[0-9a-z]+", text)) == {newpub}           # names ONLY the new key
    # the regression: the additive writer can now find the recipient + add a break-glass key without error
    w = keygen.add_recipient("age1breakglassbreakglassbreakglassbreakglassbreakglassbg00",
                             ["base_recipients", "ops_recipients"])
    assert w["error"] is None and w["changed"]


def test_recipient_re_tolerates_a_trailing_comment():
    """F4 unit: the recipient-line regex must accept a trailing '# comment' (the template's '# REPLACE'); without
    it the text scanner reads a commented recipient block as empty (the root of the no_recipients failure)."""
    m = keygen._RECIPIENT_RE.match('      - "age1abc00000000000000000000"   # REPLACE\n')
    assert m and m.group(2) == "age1abc00000000000000000000"
    assert keygen._RECIPIENT_RE.match('      - age1bare0000000000000000000\n')      # unquoted, no comment


def test_the_apply_result_names_the_run_id_it_was_given(sops_repo):
    """Verifies: the apply result's promote step is `promote_hint` for the run id the route minted, with no
    `<run_id>` placeholder left; the plan (no id exists yet) keeps the placeholder; `next_steps(..., staged=False)`
    has no promote step, and an unknown role has no steps. Guards against: an operator copying a placeholder into
    the promote task although the id was known two lines earlier (F12), and a promote step for a write that staged
    nothing (the key was already a recipient, or the deploy commits to main)."""
    from kontroll.service.pending import promote_hint
    res = keygen.apply_keygen_plan("semaphore", run_id="0123456789ab")
    assert any(promote_hint("0123456789ab") in s for s in res["next_steps"])
    assert not any("<run_id>" in s for s in res["next_steps"])
    assert any(promote_hint("<run_id>") in s for s in keygen.build_keygen_plan("semaphore")["next_steps"])
    unstaged = keygen.next_steps("semaphore", "0123456789ab", staged=False)
    assert unstaged and not any("promote" in s.lower() for s in unstaged)
    assert keygen.next_steps("not_a_role") == []
