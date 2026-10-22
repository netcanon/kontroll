"""Secret-onboarding route — /secrets/{domain} (+ GET fields). The guided secret-entry surface (D).

GET /secrets/{domain}/fields returns the domain's form fields + which are already set (NAMES only, never a
value). POST /secrets/{domain} PROPOSES (apply:false — the resolved plan VIEW, no values) or APPLIES
(apply:true — encrypt the values into the SOPS domain, commit + STAGE proposed/<run_id>, audited). Privileged
(token + the fail-closed audit) by the shared /secrets prefix. The request body carries the secret VALUES;
they are encrypted into SOPS and are NEVER returned, logged, or committed in plaintext — only field NAMES + a
source label appear. 404 on an unregistered domain. The deployed surface needs an age key to encrypt
(onboard-gui mounts one; the no-key API returns a clean error). See secret-forms/README.md + SECURITY.md.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.auth import Principal, audit_action, require_token
from kontroll import catalog, gitio
from kontroll.service import secrets as secrets_service
from kontroll.service.pending import promote_hint

router = APIRouter(prefix="/secrets", tags=["privileged"])


class SecretIn(BaseModel):
    """A secret-entry request. `values` maps a field key -> the SECRET value (encrypted into SOPS, NEVER
    echoed or logged). apply:false returns the resolved plan view (names + source, no values); apply:true
    writes + stages. A blank value for a field that has a `generate:` spec mints one on the box."""
    values: dict = {}
    apply: bool = False
    push: bool = False
    overwrite: bool = False   # ack to clobber already-set fields (rotation); fail-closed without it (C9 M-R-B)
    regenerate: list = []     # keys to EXPLICITLY re-mint (the 'gen' button); a blank already-set field is else KEPT


def _set_names(view):
    """The field names actually being written (provided or generated) — for the audit line + commit body.
    NEVER includes a value (the view carries names + source only)."""
    return [v["key"] for v in view if v["source"] in ("provided", "generated")]


@router.get("/{domain}/fields", summary="A domain's secret form fields (names + already-set; no values)")
def fields(domain: str, request: Request, principal: Principal = Depends(require_token)) -> dict:
    """The descriptor's fields annotated with `already_set` (read-only; NAMES only, never a value). 404 if the
    domain has no secret-form descriptor. The picker uses this to render the entry form."""
    out = secrets_service.offerable_fields(domain)
    if out["error"] == "no_form":
        raise HTTPException(status_code=404, detail="no secret form for domain: %s" % domain)
    return {**out, "run_id": principal.run_id}


@router.post("/{domain}", summary="Propose (dry-run) or apply (write + stage) a domain's secrets — audited")
def secret_apply(domain: str, body: SecretIn, request: Request,
                 principal: Principal = Depends(require_token)) -> dict:
    """Dry-run (default) resolves the plan and returns the VIEW (field names + source: provided/generated/
    already_set/skipped — never a value); apply:true encrypts the values into instance/secrets/<domain>.sops.yml
    (whole-file, via stdin — no argv/temp-file leak), commits, and STAGES proposed/<run_id> (C10). 404 unknown
    domain; 422 a missing required field; 500 if the box can't encrypt (no age key). The action + field NAMES
    are audited; the values NEVER are."""
    if catalog.secret_form(domain) is None:
        raise HTTPException(status_code=404, detail="no secret form for domain: %s" % domain)
    plan = secrets_service.build_secret_plan(domain, body.values, body.regenerate)
    if (plan.get("error") or "").startswith("missing:"):
        raise HTTPException(status_code=422, detail="required field not provided: %s" % plan["error"].split(":", 1)[1])
    if plan["error"]:
        raise HTTPException(status_code=422, detail=plan["error"])
    overwrite = secrets_service.overwrite_set(domain, plan)   # NAMES of already-set fields this would clobber (C9)
    if not body.apply:
        return {"applied": False, "domain": domain, "view": plan["view"], "overwrite": overwrite,
                "run_id": principal.run_id}
    if overwrite and not body.overwrite:                      # fail CLOSED: clobbering a live secret needs an ack
        # Structured detail (NAMES as DATA, mirroring the GUI route's flat `overwrite` array) so both privileged
        # surfaces expose the clobbered fields the same way — not a prose string a client must parse (C-1).
        raise HTTPException(status_code=409,
                            detail={"error": "overwrite confirm required", "overwrite": overwrite})

    set_names = _set_names(plan["view"])
    audit_action(request, principal, "secret-apply",
                 "domain=%s fields=%s overwrite=%s push=%s" % (
                     domain, ",".join(set_names) or "none", ",".join(overwrite) or "none", body.push))
    applied = secrets_service.apply_secret_plan(plan)
    if applied["error"]:
        raise HTTPException(status_code=500,
                            detail="secret write failed: %s (is an age key present to encrypt?)" % applied["error"])
    if not applied["changed"]:
        return {"applied": True, "changed": False, "committed": False, "domain": domain,
                "view": plan["view"], "run_id": principal.run_id, "note": "nothing changed; nothing to commit"}
    git = gitio.commit_and_push(
        applied["paths"],
        ["feat(secrets): set %s via the secret-onboarding dialog" % domain,
         "Set %d field(s) on %s via the kontroll API (run_id %s). SOPS ciphertext only; values never logged." % (
             len(_set_names(plan["view"])), domain, principal.run_id)],
        push_origin=body.push, run_id=principal.run_id)   # staging network service ⇒ proposed/<run_id> (C10)
    if not git["committed"]:
        raise HTTPException(status_code=500, detail="commit failed; nothing pushed (repo may be dirty)")
    return {"applied": True, "changed": True, "committed": True, "domain": domain, "view": plan["view"],
            "paths": applied["paths"], "pushed_canonical": git["pushed_canonical"],
            "pushed_origin": git["pushed_origin"], "target_ref": git["target_ref"], "staged": git["staged"],
            "enact": secrets_service.actuation_enact_commands(domain, set_names),   # post-promote hand-off (NAMES + cmds)
            "run_id": principal.run_id,
            "next": promote_hint(principal.run_id) if git["staged"] else "committed to main"}
