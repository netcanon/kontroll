"""Onboard route — POST /onboard. The operator's one-shot over HTTP: a dry-run returns the plan;
apply performs the repo mutation (module + drop-in host + fleet-enable + creds→SOPS) and commits +
takes the commit to the local canonical (a staging push of a proposal when armed; a landing through the promote
gate otherwise — never a push of `main`). Privileged (token + audit). The highest-blast-radius route, so: dry-run
by default, credentials are accepted but NEVER returned or logged (only their var names appear), a
failed commit pushes nothing, and bootstrap (collection install + live-verify) is DELEGATED to
Semaphore — the API does not run Ansible (docs/api-architecture.md §8).
"""
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from api.auth import Principal, audit_action, require_token
from api.deps import Catalog, get_catalog
from kontroll import authspec, catalog, gitio, paths, probe
from kontroll.service.onboard import apply_onboard_plan, build_onboard_plan, no_backend_message
from kontroll.service.pending import promote_hint

# What a `no_backend` answer tells an API or GUI caller to do (the CLI's names its own flag).
NO_BACKEND_REMEDY = ("if it is a plain SSH host, onboard it from the Generic SSH host search result (search: ssh), "
                     "or pass an explicit backend")

router = APIRouter(tags=["privileged"])


class OnboardIn(BaseModel):
    """An onboard request. Credentials (username/password/api_token/ssh_private_key) are encrypted into SOPS
    during apply and are NEVER echoed back or logged — only their derived var names appear (in the plan).
    `ssh_private_key` is the unified password-OR-key seam: a pasted PEM, SOPS-stored per host, materialized to a
    0600 file at fleet-play time (the device admin/connection credential; an alternative or fallback to password)."""
    collection: str
    key: str
    group: str
    host: str
    host_name: Optional[str] = None
    secrets: str = "network"
    backend: Optional[str] = None
    # F1: the DERIVED per-backend credential fields, a {field: value} map (the form sends exactly the fields
    # authspec derived for the picked backend). The legacy scalars below are kept so an existing caller still
    # works — they are folded into `creds` when `creds` is None. Values are SOPS-encrypted at apply, never echoed.
    creds: Optional[Dict[str, str]] = None
    username: Optional[str] = None
    password: Optional[str] = None
    api_token: Optional[str] = None
    ssh_private_key: Optional[str] = None
    apply: bool = False
    push: bool = False
    overwrite: bool = False   # replace an existing divergent module/host drop-in (else a collision is a 409)
    deep: bool = False        # F1 TIER-B: sharpen the cred fields from the module argument_spec (install-confined)


def _plan_view(plan: dict) -> dict:
    """The client-safe plan: the module + drop-in host blocks, the rel paths, and the cred var
    NAMES — never the credential values (which live only in the plan's in-memory creds_to_set)."""
    return {"collection": plan["collection"], "key": plan["key"], "group": plan["group"],
            "backend": plan["backend"], "role": plan["role"], "host_name": plan["host_name"],
            "module_reuse": plan.get("module_reuse", False),   # reusing a curated class — add host only (#124)
            "module": plan["module"], "host_block": plan["host_block"],
            "paths": {"module": plan["mod_path"], "inventory": plan["inv_path"]},
            "creds": sorted(plan["creds_to_set"].keys()),
            # F1: the DERIVED credential-field descriptors (names/kinds/labels/flags — NEVER values, wire-projected
            # by authspec.public_descriptor) so the GUI renders the RIGHT per-backend fields; cred_source = shallow
            # (the backend's auth: block) | fallback (the generic union — a backend declaring no shape).
            "cred_fields": [authspec.public_descriptor(cf) for cf in (plan.get("cred_fields") or [])],
            "cred_source": plan.get("cred_source"),
            # F2: the honest capability GAP — vendor/credentialed rungs this blind class can't auto-derive (names +
            # why only, non-secret); advisory + NON-GATING (INVARIANT D*). Empty for a full-parity / reused class.
            "capability_gap": plan.get("capability_gap") or [],
            "provisioning": plan.get("provisioning") or []}   # advisory cred-prereqs (role NAMES only, non-gating)


@router.post("/onboard", summary="Onboard a device (dry-run, or apply — audited)")
def onboard(body: OnboardIn, request: Request,
            principal: Principal = Depends(require_token),
            cat: Catalog = Depends(get_catalog)) -> dict:
    """Dry-run (default) returns the onboard PLAN; `apply: true` writes the module + drop-in host +
    fleet-enable + encrypts creds to SOPS, then commits + stages a proposal on the local canonical (or, outside
    staging, lands on `main` through the promote gate — `gitio.commit_and_push`) (audited).
    404 if the collection isn't installed; 422 if no backend auto-matches. A 409 is told apart by its `code`:
    a drop-in that exists with different content (no `code`; retry with `overwrite`), or `repository_data` — the
    repository's own data or layout stopped the request, and a retry will not help. A re-onboard with no change
    commits nothing. Bootstrap (install + live-verify) is a Semaphore task, not the API's."""
    creds = body.creds
    if creds is None:   # legacy scalar caller — fold the four named creds into the F1 map (back-compat)
        creds = {f: v for f, v in (("username", body.username), ("password", body.password),
                                   ("api_token", body.api_token), ("ssh_private_key", body.ssh_private_key))
                 if v is not None}
    try:
        plan = build_onboard_plan(body.collection, body.key, body.group, body.host,
                                  host_name=body.host_name, secrets=body.secrets, backend=body.backend,
                                  creds=creds, backends=cat.backends, deep=body.deep)
    except ValueError as e:
        # A catchable planner guard fired BEFORE any write: the request boundary (C22 — a field that is not a closed
        # charset), MF-S2 domain-confinement, or a partially-filled required auth_set (both operator-input errors —
        # the latter more reachable via F1 TIER-B's derived multi-field auth_sets, e.g. proxmox
        # api_user+token_id+token_secret). A clean 422, never a 500 (mirrors WriteConflict → 409); no creds were
        # written (the guards run pre-`creds_to_set`). AUDITED by field NAME, never value: a refused `key` of
        # `../../etc` must not be copied into the audit log, and a refusal that leaves no trace is a blind spot.
        boundary = isinstance(e, paths.RequestBoundaryError)
        audit_action(request, principal, "onboard-refuse", "reason=%s field=%s" % (
            "request-boundary" if boundary else "planner-guard", getattr(e, "field", "input")))
        # the SAME body the app-wide handler gives every other route (api/main.py): the message and a code derived
        # from the field — this route catches the planner's other guards too, so it builds the answer itself
        return JSONResponse(status_code=422, content={"detail": str(e),
                                                      "code": e.code if boundary else "invalid_request"})
    if plan["error"] == "not_installed":
        raise HTTPException(status_code=404, detail="install the collection first: %s" % body.collection)
    if plan["error"] == "no_backend":
        # The collection showed nothing a backend kontroll can run classifies on (S24: raw_ssh no longer catches it):
        # the answer names what it showed; the audit line names only the reason — nothing from the collection.
        audit_action(request, principal, "onboard-refuse", "reason=no-backend")
        return JSONResponse(status_code=422, content={
            "detail": no_backend_message(plan, body.collection, NO_BACKEND_REMEDY), "code": "no_backend"})
    if not body.apply:
        return {"applied": False, "plan": _plan_view(plan), "run_id": principal.run_id}

    audit_action(request, principal, "onboard-apply",
                 "collection=%s key=%s group=%s host=%s creds=%s push=%s" % (
                     body.collection, body.key, body.group, body.host,
                     ",".join(sorted(plan["creds_to_set"])) or "none", body.push))
    try:
        applied = apply_onboard_plan(plan, overwrite=body.overwrite)
    except gitio.WriteConflict as e:
        # A drop-in already exists divergently (e.g. the key collides with a shipped module) — clean 409,
        # never a SystemExit that would crash the worker. Retry with overwrite=true to replace it.
        raise HTTPException(status_code=409, detail=str(e))
    if not applied["changed"]:
        return {"applied": True, "changed": False, "committed": False,
                "host_name": plan["host_name"], "run_id": principal.run_id,
                "note": "nothing changed (already onboarded); nothing to commit"}
    git = gitio.commit_and_push(
        applied["paths"],
        ["feat(onboard): %s -> %s via %s backend" % (body.collection, body.key, plan["backend"]),
         "Onboarded via the kontroll API (run_id %s). Host %s in group %s; run ping.yml --limit HOST to verify." % (
             principal.run_id, plan["host_name"], plan["group"])],   # the plan's group: a reuse ADOPTS the class's
        push_origin=body.push, run_id=principal.run_id)   # staging network service ⇒ proposed/<run_id> (C10)
    if not git["committed"]:
        raise HTTPException(status_code=500, detail="commit failed; nothing pushed (repo may be dirty)")
    return {"applied": True, "changed": True, "committed": True,
            "pushed_canonical": git["pushed_canonical"], "pushed_origin": git["pushed_origin"],
            "target_ref": git["target_ref"], "staged": git["staged"],
            "host_name": plan["host_name"], "run_id": principal.run_id,
            "next": (promote_hint(principal.run_id) + "; then bootstrap (collection install + live-verify) in "
                     "Semaphore") if git["staged"]
                    else "bootstrap (collection install + live-verify) is a Semaphore task"}


@router.get("/onboard/cred-fields", summary="Derived credential fields for a backend (read-only, names only)")
def onboard_cred_fields(backend: str, secrets: str = "network", collection: Optional[str] = None,
                        deep: bool = False,
                        principal: Principal = Depends(require_token),
                        cat: Catalog = Depends(get_catalog)) -> dict:
    """The DERIVED CredField descriptors (names/kinds/labels/flags — NEVER values) for `backend`, so the onboard
    form can render the RIGHT per-backend credential inputs (SSH login for a switch, a token for a REST device)
    the instant a result is picked — before any plan or probe. When `collection` resolves to a curated device-class
    (the reuse path), that class's OWN auth: block takes precedence — so a proxmox result shows its coherent
    api-token auth_set (grouped, names lined up with the pve-exporter), not the generic single token box (F1 seam
    S1). A backend/class with no auth: block falls back to the generic union (cred_source=fallback) so the form is
    never blocked. `deep=true` SHARPENS the shape to the EXACT argument_spec fields (F1 TIER-B, Rung 3) — install-
    confined (MF-S1: only for a locally-installed collection; a Galaxy result is never introspected here) and
    fail-safe (MF-S6). Read-only, no creds: pure schema, auth-gated like the rest of this router; a read surface
    never gates onboarding (INVARIANT D*)."""
    paths.component(secrets, "secrets domain")     # it is written into every descriptor this answers with
    bdef = next((b for b in cat.backends if b.get("name") == backend), {})
    module = catalog.module_for_collection(collection) if collection else None
    # TIER-B is worth the introspection only for an INSTALLED collection (MF-S1 also fail-closes inside authspec);
    # deep-probe it once for the candidate module list, else stay on the coarse TIER-A shape (no ansible-doc cost).
    modules = None
    if deep and collection and collection in catalog.local_installed():
        modules = authspec.auth_candidate_modules(probe.deep_probe(collection))
    out = authspec.derive_auth(bdef, secrets, module=module, deep=deep, coll=collection, modules=modules)
    return {"backend": backend, "secrets": secrets,
            "cred_fields": [authspec.public_descriptor(cf) for cf in out["fields"]],
            "cred_source": out["source"]}
