"""Propose-then-promote staging (SECURITY.md C10) — gitio._push_target / commit_and_push.

Pins the network-service write posture decided in docs/privileged-mutation-enablement.md: a service deployed
with KONTROLL_STAGE_PUSHES pushes a per-request `proposed/<run_id>` STAGING ref (never `main`). Guards that a
staging service cannot write `main` directly — through the `gitio._run` seam, offline. The other half of the
control — the promote moves `main` only as a fast-forward, only to the commit that was judged — is held against
real git in tests/unit/test_promote_cas.py (a mocked seam cannot say which transaction git accepts).
"""
import pytest

from kontroll import gitio

pytestmark = pytest.mark.unit


@pytest.fixture
def rec(monkeypatch):
    """Record git commands through the `_run` seam; every command 'succeeds' (rc 0)."""
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    return calls


def _push_refspec(calls):
    return next(c for c in calls if c[:3] == ["git", "push", "local"])[3]


def test_direct_push_to_main_when_unstaged(rec, monkeypatch):
    """With KONTROLL_STAGE_PUSHES unset, the canonical push targets `main` (the operator-CLI / non-staging
    deploy path, unchanged even if a run_id is passed). Guards a regression that stages when it shouldn't."""
    monkeypatch.delenv("KONTROLL_STAGE_PUSHES", raising=False)
    git = gitio.commit_and_push(["x"], ["msg"], run_id="000000abc123")
    assert git["target_ref"] == "main" and git["staged"] is False
    assert _push_refspec(rec) == "HEAD:refs/heads/main"


def test_stages_a_proposed_ref_when_enabled(rec, monkeypatch):
    """With KONTROLL_STAGE_PUSHES=1 + a run_id, the push targets `proposed/<run_id>`, NEVER `main` — the
    network service can only PROPOSE (C10). Guards the core safety property: a staging service never writes
    main, so a leaked token only stages a proposal."""
    monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    git = gitio.commit_and_push(["x"], ["msg"], run_id="000000000042")
    assert git["staged"] is True and git["target_ref"] == "proposed/000000000042"
    assert _push_refspec(rec) == "HEAD:refs/heads/proposed/000000000042"
    assert all("refs/heads/main" not in c[-1] for c in rec if c[:3] == ["git", "push", "local"])


def test_staging_without_a_run_id_is_fail_closed(rec, monkeypatch):
    """Staging needs a run_id to name the proposal; without one the push is REFUSED (raises) BEFORE any mutation —
    never a silent fallback to a direct `main` push. Guards the fail-open hole an adversarial review flagged: a
    future route/refactor that reaches commit_and_push without threading run_id must error loudly, not quietly
    write main under the token. (The raise happens before `git add`, so the clone is left untouched.)"""
    monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    with pytest.raises(ValueError):
        gitio.commit_and_push(["x"], ["msg"])
    assert not rec                                  # nothing ran: no add/commit/push before the refusal


def test_from_env_refuses_a_token_without_staging(monkeypatch):
    """C10 fail-closed coupling: a network service armed with KONTROLL_API_TOKEN but NO KONTROLL_STAGE_PUSHES
    would let the token push `main` directly (gitio._push_target) — so from_env REFUSES to start it. Guards the
    decoupled-misconfiguration hole the review flagged (hand-edited .env / partial deploy). With both set, it
    builds normally."""
    from api import settings
    monkeypatch.setenv("KONTROLL_API_TOKEN", "t0ken")
    monkeypatch.delenv("KONTROLL_STAGE_PUSHES", raising=False)
    with pytest.raises(RuntimeError):
        settings.from_env()
    monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    assert settings.from_env().api_token == "t0ken"        # both set ⇒ armed + staging, allowed
