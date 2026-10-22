"""Capability route — /capability/{cap} (+ /suggest). The standalone secondary-capability dialog's backend.

ONE route handles EVERY secondary capability (telemetry today, backup at Phase 8): `cap` is a PATH SEGMENT,
never an `if cap==…` branch. GET /capability/{cap}/suggest is the read-only Stage-0 detection; POST
/capability/{cap} PROPOSES (apply:false — a pure plan + an anti-drift token) or PROMOTES (apply:true — the
plan-hash-gated, audited, scoped-commit write). Privileged (token + the fail-closed audit) by the shared
/capability prefix. 404 on an unregistered cap. The API runs NO play — ENACT (making metrics flow) is the
operator's, returned as commands in the plan. INVARIANT D*: this surface is strictly-after onboarding and
never gates it. See docs/observability/secondary-capability-dialog.md §3.5.
"""
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.auth import Principal, audit_action, require_token
from api.deps import Catalog, get_catalog
from kontroll import gitio
from kontroll.service import capability
from kontroll.service.pending import promote_hint

router = APIRouter(prefix="/capability", tags=["privileged"])

# error code -> HTTP status (a propose/promote error string the service returns -> the right 4xx).
_STATUS = {"no_capability": 404, "not_onboarded": 404, "unknown_method": 422,
           "bad_param": 422, "already_declared": 409, "drift": 409}


class CapabilityIn(BaseModel):
    """A propose/promote request. `selection` is the dialog's choices (capability-shaped — e.g. telemetry's
    {method, params, dashboards}); `token` is the propose-time plan hash echoed back at promote (the anti-drift
    gate). No credential is ever carried here (telemetry has none; a future cap's secret stays SOPS)."""
    key: str
    selection: dict = {}
    apply: bool = False
    token: Optional[str] = None
    push: bool = False


def _fail(error: str, detail: Any = None) -> HTTPException:
    return HTTPException(status_code=_STATUS.get(error, 422), detail=str(detail or error))


def _plan_view(cap: str, plan: dict) -> dict:
    """The client-safe plan: the declared selection, the repo-rel paths the promote will commit, and the enact
    commands — never a credential (telemetry carries none; this stays true for every capability by contract)."""
    return {"cap": cap, "key": plan["key"], "method": plan.get("method"),
            "params": plan.get("params", {}), "dashboards": plan.get("dashboards", []),
            "paths": plan["paths"], "enact": plan["enact"],
            "links": plan.get("links") or []}   # capability links (telemetry → Grafana, #122) — generic passthrough


@router.get("/{cap}/suggest", summary="Detect a capability's fit for an onboarded class (read-only)")
def suggest(cap: str, key: str, request: Request,
            principal: Principal = Depends(require_token),
            cat: Catalog = Depends(get_catalog)) -> dict:
    """Read-only Stage-0 detection: the suggester cell + candidates, the methods the picker may offer, and
    what's already declared for the class. 404 if `cap` is unregistered or `key` is not onboarded. Writes
    nothing (a suggester could never gate onboarding — INVARIANT D*)."""
    view = capability.suggest_view(cap, key, vectors=cat.vectors, caps=cat.capabilities)
    if view.get("error"):
        raise _fail(view["error"], "no such capability: %s" % cap if view["error"] == "no_capability"
                    else "not onboarded: %s" % key)
    return {**view, "run_id": principal.run_id}


@router.post("/{cap}", summary="Propose (dry-run) or promote (apply — audited) a capability for a class")
def capability_apply(cap: str, body: CapabilityIn, request: Request,
                     principal: Principal = Depends(require_token),
                     cat: Catalog = Depends(get_catalog)) -> dict:
    """Dry-run (default) returns the PURE plan + an anti-drift `token`; `apply: true` re-verifies the token,
    writes the capability block, regenerates its artifacts, and commits ONLY the plan's paths (audited). 404
    unknown cap; 409 on drift / already-declared; 422 on a bad selection. The API runs no play — the returned
    enact commands are the operator's to make it live."""
    if capability.get_descriptor(cap, cat.capabilities) is None:
        raise _fail("no_capability", "no such capability: %s" % cap)

    if not body.apply:
        plan = capability.propose(cap, body.key, body.selection, caps=cat.capabilities)
        if plan.get("error"):
            raise _fail(plan["error"], plan.get("detail"))
        return {"applied": False, "plan": _plan_view(cap, plan),
                "token": plan["plan_token"], "run_id": principal.run_id}

    audit_action(request, principal, "capability-promote",
                 "cap=%s key=%s method=%s dashboards=%d token=%s push=%s" % (
                     cap, body.key, body.selection.get("method"),
                     len(body.selection.get("dashboards") or []),
                     (body.token or "")[:16], body.push))
    out = capability.promote(cap, body.key, body.selection, body.token, caps=cat.capabilities)
    if out.get("error"):
        raise _fail(out["error"], "plan drifted since propose; re-propose" if out["error"] == "drift"
                    else out.get("detail"))
    if not out["changed"]:
        return {"applied": True, "changed": False, "committed": False, "cap": cap,
                "run_id": principal.run_id, "note": "nothing changed (already declared); nothing to commit"}
    git = gitio.commit_and_push(
        out["paths"],
        ["feat(%s): enable on %s via the capability dialog" % (cap, body.key),
         "Promoted via the kontroll API (run_id %s). Data-only write; nothing actuated until the enact "
         "commands run." % principal.run_id],
        push_origin=body.push, run_id=principal.run_id)   # staging network service ⇒ proposed/<run_id> (C10)
    if not git["committed"]:
        raise HTTPException(status_code=500, detail="commit failed; nothing pushed (repo may be dirty)")
    return {"applied": True, "changed": True, "committed": True, "cap": cap, "paths": out["paths"],
            "pushed_canonical": git["pushed_canonical"], "pushed_origin": git["pushed_origin"],
            "target_ref": git["target_ref"], "staged": git["staged"],
            "enact": out["plan"]["enact"], "links": out["plan"].get("links") or [],   # Grafana deep-links (#122)
            "run_id": principal.run_id,
            "next": ((promote_hint(principal.run_id) + "; then run the enact commands") if git["staged"]
                     else "not live yet — run the listed enact commands to make it flow")}
