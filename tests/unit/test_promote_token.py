"""promote.plan_token / verify_token — the capability-neutral propose-then-promote anti-drift gate.

These pin the contract every secondary-capability dialog leans on (docs/observability/secondary-capability-
dialog.md §3.1): a promote may only land the EXACT plan the operator was shown. They guard that the token is
PURE (so the propose-time and promote-time hashes match), that part BOUNDARIES are part of the identity (so a
regrouped write-set can't masquerade as the original), that any drift flips verification to False (the 409
case), and that a malformed/absent token is a clean mismatch rather than a crash. The gate is consistency,
not authorization — so there is deliberately no secret/salt/clock to test.
"""
import pytest

from kontroll.service import promote

pytestmark = pytest.mark.unit


def test_token_is_pure_and_hex():
    """The same parts always hash to the same 64-char hex digest — the purity the seam needs so the token
    computed at PROPOSE equals the one recomputed at PROMOTE. Guards against any accidental salt/clock/random
    creeping in (which would make every promote a spurious 409)."""
    t1 = promote.plan_token("metrics:\n- method: snmp\n", "targets-blob")
    t2 = promote.plan_token("metrics:\n- method: snmp\n", "targets-blob")
    assert t1 == t2
    assert len(t1) == 64 and all(ch in "0123456789abcdef" for ch in t1)


def test_part_boundaries_are_part_of_identity():
    """Length-framing makes ("ab","c") and ("a","bc") hash DIFFERENTLY — a part boundary is part of the
    plan's identity. Guards the subtle drift where two parts are re-split (e.g. a block bleeds into the
    artifact text) yet the naive concatenation would look unchanged."""
    assert promote.plan_token("ab", "c") != promote.plan_token("a", "bc")


def test_order_matters():
    """Reordering the parts changes the token — the write-set is an ORDERED plan (block, then artifact, then
    paths), not a set. Guards against a promote landing the same pieces in a different arrangement."""
    assert promote.plan_token("a", "b") != promote.plan_token("b", "a")


def test_verify_roundtrips_for_an_unchanged_plan():
    """verify_token(plan_token(*parts), *parts) is True — the happy path: the operator promotes the exact
    plan they proposed, so the gate opens."""
    parts = ("metrics: [snmp]", "regenerated-targets", "modules/cisco_ios/module.yml")
    assert promote.verify_token(promote.plan_token(*parts), *parts) is True


def test_any_drift_fails_verification():
    """If ANY part changes between propose and promote, verification is False (the route returns 409 and the
    operator must re-propose). Guards the core safety property — a promote cannot land bytes the operator
    never reviewed (e.g. someone else edited the target file in between)."""
    token = promote.plan_token("block-v1", "artifact-v1")
    assert promote.verify_token(token, "block-v1", "artifact-v2") is False   # artifact drifted
    assert promote.verify_token(token, "block-v2", "artifact-v1") is False   # block drifted


def test_malformed_token_is_a_clean_mismatch():
    """An empty, None, non-string or non-ASCII token verifies as False rather than raising — a malformed promote
    request is REFUSED, never a 500. Guards the route from an attacker/bug sending a junk token (`hmac.compare_digest`
    raises TypeError on a non-ASCII str)."""
    assert promote.verify_token("", "a") is False
    assert promote.verify_token(None, "a") is False
    assert promote.verify_token(12345, "a") is False
    assert promote.verify_token("not-the-hash", "a") is False
    assert promote.verify_token("töken", "a") is False


def test_none_part_is_well_defined_not_a_crash():
    """A None part hashes as empty (a well-defined absence), so an optional plan section being absent is a
    stable token, not an exception — and is distinct from an empty-string part only via the framing it shares
    with "" (both length-0), which is the intended equivalence (absent == empty for hashing)."""
    assert promote.plan_token("a", None, "b") == promote.plan_token("a", "", "b")
    assert promote.plan_token(None) == promote.plan_token("")
