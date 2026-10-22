"""Actuation route — POST /actuation/{key}. The FIRST app-store WRITE verb (R5).

A dedicated PRIVILEGED prefix (token + the fail-closed audit + the per-token rate budget), SEPARATE from the
`/units` inquiry prefix (the write-free search/configure/preview reads) — so the prefix-based rate-limit
classifier and the auth boundary cleanly distinguish the write from the reads (mirrors `/capability` + `/secrets`).
`apply: false` PROPOSES (a pure plan + an anti-drift token); `apply: true` STAGES the configured unit's values as a
proposal on `proposed/<run_id>` via the ONE write seam `gitio.commit_and_push(run_id=…)` — never `main`, and this
route NEVER imports `promote_ref` (C10: the trusted second key is the Semaphore-admin promote). The API stages
CONFIG only (the unit's curated, non-secret values); nothing is actuated until the operator promotes the proposal
and runs the unit in Semaphore. Mirrors `api/routes/capability.py`'s propose/promote shape. SEC-2: a unit may not
carry a secret, so no value staged here is ever a credential, and the audit line carries names/counts/run_id only.
"""
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.auth import Principal, audit_action, require_token
from kontroll import gitio
from kontroll.service.pending import promote_hint
from kontroll.service.actuation import (
    build_actuation_plan, build_create_unit_plan, stage_create_unit, stage_plan)

router = APIRouter(prefix="/actuation", tags=["privileged"])

# service error string -> HTTP status (the propose/stage error -> the right 4xx).
_STATUS = {"no_unit": 404, "invalid": 422, "drift": 409}
# the create flow's richer error set: no_module/invalid = bad request data; not_enabled/exists/drift = state
# conflict; tier_cap = a POLICY refusal (an unsigned app-store install may not reach the highest-blast tier).
_CREATE_STATUS = {"invalid": 422, "no_module": 422, "not_enabled": 409, "exists": 409, "tier_cap": 403, "drift": 409}


class StageIn(BaseModel):
    """A propose/stage request for a configured actuation unit. `values` are the curated configure values
    (re-validated server-side; never a secret — SEC-2); `token` is the propose-time plan hash echoed back at
    stage (the anti-drift gate); `apply` false = dry-run (the plan paths + a token), true = stage the proposal."""
    values: dict[str, Any] = {}
    apply: bool = False
    token: Optional[str] = None
    push: bool = False


class CreateIn(BaseModel):
    """A propose/create request to AUTHOR an actuation unit from a searched collection. The descriptor is rendered
    + validated server-side; the device-class/inventory-group are DERIVED from the declaring module (not client
    input). `apply` false = dry-run (the derived key + the unit.yml path + a token), true = stage the descriptor.
    `token` is the propose-time plan hash echoed back at create (the anti-drift gate). No field is ever a secret."""
    collection: str
    kind: str
    name: str
    version: str
    blast_radius: str
    apply: bool = False
    token: Optional[str] = None
    push: bool = False


def _detail(error: str, key: str, errors: Any = None) -> Any:
    """The HTTP detail for a service error — the per-knob errors for 'invalid', a re-propose hint for 'drift',
    else a clean not-found string. Never echoes a submitted value."""
    if error == "invalid":
        return {"errors": errors or {}}
    if error == "drift":
        return "plan drifted since propose; re-propose"
    return "no actuation unit '%s'" % key


def _create_detail(out: dict) -> Any:
    """The HTTP detail for a create-flow service error — the per-field errors for 'invalid', a pointed message for
    the Tier-cap / collision / unresolved-class refusals, a re-propose hint for 'drift'. Never echoes a credential
    (no create input is a secret)."""
    err = out.get("error")
    if err == "invalid":
        return {"errors": out.get("errors") or {}}
    if err == "drift":
        return "plan drifted since propose; re-propose"
    if err == "tier_cap":
        return ("the %s device-class targets %s, the highest-blast tier — an app-store install there needs a "
                "signed source (public Galaxy is unsigned). Refused (never-brick + blast-radius)."
                % (out.get("device_class"), out.get("inventory_group")))
    if err == "exists":
        return "a unit '%s' already exists — configure or disable it, don't overwrite" % out.get("key")
    if err == "not_enabled":
        return "the device-class '%s' is not an enabled module (instance/fleet.yml)" % out.get("device_class")
    if err == "no_module":
        return "no device-class module declares collection '%s'" % out.get("collection")
    return "create failed"


# Declared BEFORE the parametrized POST /{key} so the static path wins (Starlette matches in declaration order);
# "create" is a reserved key (service/actuation guards it) so /{key} never needs to serve it.
@router.post("/create", summary="Propose or create (apply — audited) an actuation unit from a searched collection")
def create(body: CreateIn, request: Request, principal: Principal = Depends(require_token)) -> dict:
    """Dry-run (default) renders + validates the unit descriptor and returns the DERIVED key, the unit.yml path,
    and an anti-drift `token`, writing nothing; `apply: true` re-verifies the token, writes the descriptor, and
    stages ONLY that path to `proposed/<run_id>` (audited `actuation-create`). 422 bad inputs / no declaring class;
    409 collision / not-enabled / drift; 403 the Tier-cap (unsigned install onto edge_firewall/core_switch). The
    API runs no play — the operator promotes (the trusted second key) then configures + runs the unit."""
    inputs = {"collection": body.collection, "kind": body.kind, "name": body.name,
              "version": body.version, "blast_radius": body.blast_radius}
    if not body.apply:
        plan = build_create_unit_plan(inputs)
        if plan.get("error"):
            raise HTTPException(_CREATE_STATUS.get(plan["error"], 422), detail=_create_detail(plan))
        return {"applied": False, "key": plan["key"], "paths": plan["paths"], "token": plan["plan_token"],
                "device_class": plan["device_class"], "inventory_group": plan["inventory_group"],
                "blast_radius": plan["blast_radius"], "version": plan["version"], "run_id": principal.run_id}

    audit_action(request, principal, "actuation-create",
                 "collection=%s kind=%s name=%s blast=%s token=%s push=%s"
                 % (body.collection, body.kind, body.name, body.blast_radius, (body.token or "")[:16], body.push))
    out = stage_create_unit(inputs, body.token)
    if out.get("error"):
        raise HTTPException(_CREATE_STATUS.get(out["error"], 422), detail=_create_detail(out))
    key = out["plan"]["key"]
    if not out["changed"]:
        return {"applied": True, "changed": False, "committed": False, "key": key,
                "run_id": principal.run_id, "note": "descriptor unchanged; nothing to stage"}
    git = gitio.commit_and_push(
        out["paths"],
        ["feat(actuation): create app-store unit %s" % key,
         "Author the actuation unit descriptor for %s via the kontroll API (run_id %s). Config-only write "
         "(no value, no secret); nothing installed/actuated until the operator promotes the proposal, then "
         "configures + runs the unit in Semaphore." % (key, principal.run_id)],
        push_origin=body.push, run_id=principal.run_id)   # staging network service ⇒ proposed/<run_id> (C10)
    if not git["committed"]:
        raise HTTPException(status_code=500, detail="commit failed; nothing pushed (repo may be dirty)")
    return {"applied": True, "changed": True, "committed": True, "key": key, "paths": out["paths"],
            "device_class": out["plan"]["device_class"], "inventory_group": out["plan"]["inventory_group"],
            "pushed_canonical": git["pushed_canonical"], "target_ref": git["target_ref"], "staged": git["staged"],
            "run_id": principal.run_id,
            "next": ((promote_hint(principal.run_id) + "; then configure + run the unit in Semaphore")
                     if git["staged"]
                     else "committed to main — configure + run the unit in Semaphore")}


@router.post("/{key}", summary="Propose or stage (apply — audited) a configured actuation unit")
def stage(key: str, body: StageIn, request: Request,
          principal: Principal = Depends(require_token)) -> dict:
    """Dry-run (default) returns the plan's `paths` + an anti-drift `token`, writing nothing; `apply: true`
    re-verifies the token, writes the unit's configure-values file, and stages ONLY that path to
    `proposed/<run_id>` (audited). 404 no unit; 422 invalid values; 409 on drift. The API runs no play — the
    operator promotes (the trusted second key) then runs the unit in Semaphore."""
    if not body.apply:
        plan = build_actuation_plan(key, body.values)
        if plan.get("error"):
            raise HTTPException(_STATUS.get(plan["error"], 422), detail=_detail(plan["error"], key, plan.get("errors")))
        return {"applied": False, "key": key, "paths": plan["paths"],
                "token": plan["plan_token"], "run_id": principal.run_id}

    audit_action(request, principal, "actuation-stage",
                 "key=%s vars=%d token=%s push=%s" % (key, len(body.values or {}), (body.token or "")[:16], body.push))
    out = stage_plan(key, body.values, body.token)
    if out.get("error"):
        raise HTTPException(_STATUS.get(out["error"], 422), detail=_detail(out["error"], key, out.get("errors")))
    if not out["changed"]:
        return {"applied": True, "changed": False, "committed": False, "key": key,
                "run_id": principal.run_id, "note": "values unchanged; nothing to stage"}
    git = gitio.commit_and_push(
        out["paths"],
        ["feat(actuation): stage configure values for %s via the app-store" % key,
         "Staged via the kontroll API (run_id %s). Config-only write; nothing actuated until the operator "
         "promotes the proposal + runs the unit in Semaphore." % principal.run_id],
        push_origin=body.push, run_id=principal.run_id)   # staging network service ⇒ proposed/<run_id> (C10)
    if not git["committed"]:
        raise HTTPException(status_code=500, detail="commit failed; nothing pushed (repo may be dirty)")
    return {"applied": True, "changed": True, "committed": True, "key": key, "paths": out["paths"],
            "pushed_canonical": git["pushed_canonical"], "target_ref": git["target_ref"], "staged": git["staged"],
            "run_id": principal.run_id,
            "next": ((promote_hint(principal.run_id) + "; then run the unit in Semaphore") if git["staged"]
                     else "committed to main — run the unit in Semaphore")}
