"""SECURITY.md's break-glass claim must match what `kontroll-init --fresh` actually produces.

C1-FALSE (2026-07-28 fleet-restore review). `SECURITY.md` C1 stated:

    "every secret is encrypted to **two** age recipients — the control-VM operational key and an offline
     break-glass recovery key ... losing the VM key is recoverable, not catastrophic"

and the accepted-risks table repeated it. Both were false: every `key_groups` in the shipped
`instance.example/.sops.yaml` named exactly ONE recipient, `--fresh` wrote the same shape, and both live boxes named
only their own control key. A security document claiming a control that does not exist is worse than one admitting
the gap, because decisions get made on it — "losing the VM key is recoverable" is the sentence that makes destroying
the only holder of a key sound safe.

T11 (S12). `--fresh` now requires a break-glass choice and applies it before the first secret is written, so the
template keeps one placeholder by design and is no longer the oracle. The claim is three-state: an instance created
by `--fresh` has a second recipient unless it was created with `--no-break-glass`; an instance created earlier has
one until docs/break-glass.md is run on it; and the tool counts recipients but cannot know whether anyone kept the
private half. So the oracle here is what `--fresh` writes for each choice — the real scaffold, the real seed and the
real additive writer, run in a scratch root with only the key mint and sops stubbed — and the test stays
BIDIRECTIONAL: should `--fresh` stop producing a second recipient, the document is required to say break-glass is
not implemented again.
"""
import importlib.util
import os
import re
import shutil

import pytest
import yaml

from kontroll import paths
from kontroll.service import keygen
from kontroll.service import secrets as secrets_service

pytestmark = pytest.mark.unit

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TEMPLATE = os.path.join(_ROOT, "instance.example", ".sops.yaml")
_SECURITY = os.path.join(_ROOT, "SECURITY.md")
# The recipient is bech32 with a valid checksum (kontroll-init checks it), one byte short of a real key (leak guard).
_CHOICES = {"recipient": ["--break-glass-recipient", "age1dphkuetnw3uj6cnjv4skkemvv9ehxqqqqqqqqqqqqqqqqqqqqqahs79q"],
            "mint": ["--mint-break-glass"],
            "none": ["--no-break-glass"]}
# A sentence that claims the control key's loss is survivable must carry the condition that makes it true.
_CLAIM = re.compile(r"\brecoverable\b|\bsurvivable\b|\b(encrypted|wrapped) to (both|two)\b|every secret ha[sd] two "
                    r"recipients|no longer (catastrophic|a single point)|\+\s*(an\s+)?offline break-glass", re.I)
# The condition must govern the recipient's existence ("where a break-glass recipient was added"), not merely
# co-occur with it ("if the control key is lost … with the break-glass key").
_GUARD = re.compile(r"\b(where|if|unless|only where|once)\b[^.]*?\b(break-glass|second) recipient\b[^.]*?"
                    r"\b(is|was|were|has been)\s+(added|chosen|kept)\b", re.I)
# A sentence that reports what the document once said.
_HISTORY = re.compile(r"\b(once said|previously claimed|was false)\b", re.I)
_BREAK_GLASS_DOC = os.path.join(_ROOT, "docs", "break-glass.md")


def _load_init():
    """scripts/kontroll-init.py, loaded by path (its name is not importable)."""
    spec = importlib.util.spec_from_file_location("kontroll_init_honesty",
                                                  os.path.join(_ROOT, "scripts", "kontroll-init.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fresh_counts(tmp_path, monkeypatch):
    """{choice: the fewest recipients any rule of the new instance/.sops.yaml names} for each break-glass choice of
    `kontroll-init --fresh`, each run in its own scratch root."""
    ki = _load_init()
    monkeypatch.setattr(ki, "ensure_control_key", lambda kf: ("age1honestycontrolpub000000000000000000000000000000000", True))
    monkeypatch.setattr(ki, "_stdout_is_terminal", lambda: True)
    monkeypatch.setattr(keygen, "generate_keypair",
                        lambda: {"public": "age1honestymintedpub0000000000000000000000000000000000",
                                 "private": "AGE-SECRET-KEY-1FAKEHONESTY"})
    monkeypatch.setattr(secrets_service, "_existing_keys", lambda d: set())
    monkeypatch.setattr(secrets_service, "build_secret_plan", lambda d, s: {"error": None, "view": []})
    monkeypatch.setattr(secrets_service, "apply_secret_plan", lambda p: {"changed": False, "error": None, "paths": []})
    counts = {}
    for choice, flags in _CHOICES.items():
        root = tmp_path / choice
        shutil.copytree(os.path.join(_ROOT, "instance.example"), root / "instance.example")
        monkeypatch.setattr(paths, "ROOT", str(root))
        monkeypatch.setattr(ki, "AGE_KEY_FILE", str(root / "age" / "keys.txt"))
        assert ki.main(["--fresh", "--trust-mode", "separated"] + flags) == 0, choice
        rules = keygen._resolve_recipients(open(root / "instance" / ".sops.yaml", encoding="utf-8").read())
        assert rules, "no creation_rules parsed from the new instance's .sops.yaml — the rest proves nothing"
        counts[choice] = min(len(s) for s in rules.values())
    return counts


def _security_text():
    with open(_SECURITY, encoding="utf-8") as fh:
        return fh.read()


def _section(text, heading):
    """The body of the `### <heading> —` section."""
    return text.split("### %s —" % heading, 1)[1].split("\n### ", 1)[0]


def _risk_row(text, risk):
    """The accepted-risks table row whose first cell names `risk`."""
    return next(line for line in text.splitlines() if line.startswith("| **%s**" % risk))


def unconditional_claims(text):
    """The sentences of `text` that say the control key's loss is recoverable or survivable, that every secret is
    encrypted to two recipients, that the key is no longer a single point, or that it is kept "+ offline
    break-glass" — unless a condition in the same sentence governs the second recipient's existence, or the
    sentence reports what the document once said. Markdown line breaks are joined before the text is split into
    sentences."""
    flat = re.sub(r"\s+", " ", text)
    return [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z*(`\[])", flat)
            if _CLAIM.search(s) and not (_GUARD.search(s) or _HISTORY.search(s))]


def test_the_template_recipient_count_is_visible_at_all():
    """Guard the guard: the shipped template still parses and names at least one recipient per rule — the
    placeholder `--fresh` replaces. If it stopped parsing or lost its key_groups, the seed the oracle below runs
    would have nothing to replace and every assertion here could pass on an empty instance."""
    doc = yaml.safe_load(open(_TEMPLATE, encoding="utf-8"))
    counts = {r["path_regex"]: sum(len(g.get("age") or []) for g in r.get("key_groups") or [])
              for r in doc["creation_rules"]}
    assert counts and all(n >= 1 for n in counts.values()), counts


def test_the_doc_claim_matches_what_fresh_actually_produces(tmp_path, monkeypatch):
    """THE BIDIRECTIONAL CHECK, against what `--fresh` writes for each choice. When a supplied and a minted
    break-glass key each give every rule a second recipient, C1 must say the three-state truth: a `--fresh` instance
    has one unless created with `--no-break-glass` (all three flags named), an earlier instance has one only after
    docs/break-glass.md is run, and the tool cannot know whether the private half was kept — and R-KEY-1 must stay
    listed, naming the opt-out and the runbook. Should a choice stop producing a second recipient, C1 must say
    break-glass is NOT IMPLEMENTED again. Guards the document drifting from the code in either direction."""
    counts = _fresh_counts(tmp_path, monkeypatch)
    assert counts["none"] == 1, "--no-break-glass must leave exactly the control key: %r" % counts
    text = _security_text()
    c1 = _section(text, "C1")
    if counts["recipient"] >= 2 and counts["mint"] >= 2:
        assert "NOT IMPLEMENTED" not in c1, "every --fresh choice but the opt-out gives two recipients: say so"
        for flag in ("--break-glass-recipient", "--mint-break-glass", "--no-break-glass"):
            assert flag in c1, "C1 must name the choice %s" % flag
        assert "break-glass.md" in c1, "C1 must point an instance created earlier at the procedure"
        assert re.search(r"cannot (know|verify|prove)", c1), \
            "C1 must say the tool counts recipients but cannot know the private half was kept"
        row = _risk_row(text, "R-KEY-1")
        assert "--no-break-glass" in row and "break-glass.md" in row, \
            "R-KEY-1 stays open for the instances with one recipient, and names how to close it"
    else:
        assert "NOT IMPLEMENTED" in c1, "a --fresh choice leaves one recipient: C1 must say break-glass is missing"
        assert "R-KEY-1" in text


def test_no_unconditional_recoverable_claim_survives():
    """No sentence of C1, C8, C11 or docs/break-glass.md makes the claim `unconditional_claims` reads for, unless a
    condition governs the second recipient's existence. The rule is shown to catch the two sentences that stood in
    C1 and C8 until T11, and three rewordings the first rule passed (the T11 review: no keyword, a condition on
    the loss rather than the recipient, "+ an offline"), and to pass a conditional one. Guards an unconditional
    claim — true only of an instance someone gave a second recipient — being read as true of every instance, the
    reasoning a reclaim decision would rest on (the sentence C1 carried, which the test before T11 did not see)."""
    text = _security_text()
    for heading in ("C1", "C8", "C11"):
        assert unconditional_claims(_section(text, heading)) == [], heading
    with open(_BREAK_GLASS_DOC, encoding="utf-8") as fh:
        assert unconditional_claims(fh.read()) == [], "docs/break-glass.md"
    assert len(unconditional_claims(
        "Losing it is **recoverable** via the offline break-glass key (every\nsecret is encrypted to both "
        "recipients), so it is no longer a single point. The age key is on the VM + offline break-glass, never in "
        "any of those artifacts.")) == 2
    for reworded in ("Losing the control key is no longer catastrophic: every secret is encrypted to two recipients.",
                     "If the control key is lost, every secret stays recoverable with the offline break-glass key.",
                     "The age key is kept on the VM + an offline break-glass copy, so its loss is survivable."):
        assert len(unconditional_claims(reworded)) == 1, reworded
    assert unconditional_claims("Losing it is recoverable only where a break-glass recipient was added.") == []


def test_the_correction_states_the_consequence_not_just_the_fact():
    """"One recipient" is a fact an operator can read past; "losing the key destroys every credential" is the
    decision-relevant consequence, and the part that was actively inverted before. It still holds for every instance
    created before T11 and every one created with `--no-break-glass`, so C1 keeps saying it, with the count and the
    re-wrap that closes it."""
    c1 = _section(_security_text(), "C1")
    assert "catastrophic" in c1, "the correction must name the actual consequence of key loss"
    assert "ONE" in c1, "and state the true recipient count"
    assert "sops updatekeys" in c1, "and name the remedy that would close it"


def test_the_off_box_key_copy_is_not_described_as_break_glass():
    """The fallback an instance without a second recipient has is a COPY of the single private key. Calling that
    break-glass would re-introduce the same false comfort in new words: a copy shares the single point of failure, a
    second recipient removes it. The distinction is the whole control, so C1 keeps it."""
    c1 = _section(_security_text(), "C1")
    assert "not a break-glass recipient" in c1, \
        "the off-box key copy must be explicitly distinguished from a break-glass recipient"
