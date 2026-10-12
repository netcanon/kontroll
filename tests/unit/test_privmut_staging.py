"""Propose-then-promote staging (SECURITY.md C10) — gitio._push_target / commit_and_push / promote_ref.

Pins the network-service write posture decided in docs/privileged-mutation-enablement.md: a service deployed
with KONTROLL_STAGE_PUSHES pushes a per-request `proposed/<run_id>` STAGING ref (never `main`), and
`promote_ref` fast-forwards `main` into a proposal ONLY when it is a strict fast-forward. Guards the two safety
properties — a staging service cannot write `main` directly, and a promotion cannot rewrite history — through
the `gitio._run` seam, offline. These are the covering checks C10's control claims (SECURITY.md).
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
    git = gitio.commit_and_push(["x"], ["msg"], run_id="abc123")
    assert git["target_ref"] == "main" and git["staged"] is False
    assert _push_refspec(rec) == "HEAD:refs/heads/main"


def test_stages_a_proposed_ref_when_enabled(rec, monkeypatch):
    """With KONTROLL_STAGE_PUSHES=1 + a run_id, the push targets `proposed/<run_id>`, NEVER `main` — the
    network service can only PROPOSE (C10). Guards the core safety property: a staging service never writes
    main, so a leaked token only stages a proposal."""
    monkeypatch.setenv("KONTROLL_STAGE_PUSHES", "1")
    git = gitio.commit_and_push(["x"], ["msg"], run_id="run-42")
    assert git["staged"] is True and git["target_ref"] == "proposed/run-42"
    assert _push_refspec(rec) == "HEAD:refs/heads/proposed/run-42"
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


def test_promote_fast_forwards_only(monkeypatch):
    """promote_ref advances `main` to `proposed/<run_id>` only when it's a fast-forward: the merge-base
    ancestor check passes, then it update-refs main + deletes the proposal. Guards the trusted PROMOTE half."""
    calls = []
    monkeypatch.setattr(gitio, "_run", lambda cmd, cwd=None, quiet_args=0: calls.append(list(cmd)) or 0)
    monkeypatch.setattr(gitio, "_out", lambda cmd, cwd=None: (0, "a" * 40))     # main's object id before the check
    assert gitio.promote_ref("run-7") is True
    joined = [" ".join(c) for c in calls]
    assert any("merge-base --is-ancestor refs/heads/main refs/heads/proposed/run-7" in j for j in joined)
    assert any("update-ref refs/heads/main refs/heads/proposed/run-7 " + "a" * 40 in j for j in joined), \
        "the update-ref must carry main's old object id (compare-and-swap, finding 9)"
    assert any("update-ref -d refs/heads/proposed/run-7" in j for j in joined)


def test_promote_refuses_a_non_fast_forward(monkeypatch):
    """A non-fast-forward proposal (the ancestor check fails) is REFUSED — `main` is never updated, so a
    proposal can't rewrite history (defence-in-depth with the bare repo's receive.denyNonFastForwards)."""
    updated = []

    def fake(cmd, cwd=None, quiet_args=0):
        if cmd[:2] == ["git", "merge-base"]:
            return 1                              # main is NOT an ancestor → not a fast-forward
        if cmd[:2] == ["git", "update-ref"] and "refs/heads/main" in cmd:
            updated.append(cmd)
        return 0
    monkeypatch.setattr(gitio, "_run", fake)
    monkeypatch.setattr(gitio, "_out", lambda cmd, cwd=None: (0, "a" * 40))
    assert gitio.promote_ref("run-9") is False
    assert not updated                            # main was never advanced


def test_promote_reports_success_even_if_proposal_cleanup_fails(monkeypatch, capsys):
    """If the post-FF `update-ref -d` (proposal cleanup) fails — e.g. a perms edge on the container-created ref —
    promote_ref STILL returns True (main was correctly advanced) but emits a visible note, so a half-completed
    promote (advanced-but-not-cleaned) is surfaced rather than silently reported as fully done. Guards the
    review's 'ignored cleanup rc' finding."""
    def fake(cmd, cwd=None, quiet_args=0):
        return 1 if cmd[:2] == ["git", "update-ref"] and cmd[2] == "-d" else 0
    monkeypatch.setattr(gitio, "_run", fake)
    monkeypatch.setattr(gitio, "_out", lambda cmd, cwd=None: (0, "a" * 40))
    assert gitio.promote_ref("run-5") is True
    assert "could not delete proposed/run-5" in capsys.readouterr().out


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
