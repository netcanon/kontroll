"""The FastAPI app factory.

`create_app()` builds the app; the lifespan loads the drop-in capability catalog
(vectors/overrides/backends) once into `app.state`. Each domain is one router module under
`api/routes/`; **registering it is an explicit edit here** — the import below and the tuple in
`create_app()` — so the set of mounted routes is readable in one place and a file dropped into
`api/routes/` is never mounted by accident (tests/integration/test_api.py fails for a module that
defines a router and is not registered).

Scope: read-only inquiry (search / probe / classify / units / health) and the privileged routes
(refresh, capture-exceptions, onboard, capability, secrets, actuation, audit log) behind Bearer-token
auth and a fail-closed, run_id-correlated audit. Privileged routes are fail-closed: no
KONTROLL_API_TOKEN ⇒ 503. Deployed read-only by default; under the gated `api_privileged` deploy a
privileged write to the repository can only stage `proposed/<run_id>`, never `main` (SECURITY.md C9/C10) —
`POST /refresh` rebuilds a git-ignored cache and stages nothing. Management network only, never WAN.
Run locally for dev with: `uvicorn api.main:app --reload`.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from api.audit import write_audit
from api.deps import load_catalog
from api.ratelimit import RateLimitMiddleware
from api.routes import audit as audit_routes
from api.routes import (actuation, capability, classify, exceptions, health, onboard, probe, refresh, search,
                        secrets, units)
from api.settings import from_env
from kontroll import paths


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the capability catalog (vectors/overrides/backends) once at startup — the read-only
    drop-in registries every route shares. The per-request I/O (deep_probe / local_installed /
    galaxy_search) stays in the service functions, not here."""
    app.state.catalog = load_catalog()
    yield


def _boundary_refused(request: Request, exc: paths.RequestBoundaryError) -> JSONResponse:
    """A request field failed its closed-charset check somewhere below a route (SECURITY.md C22): answer 422 with
    the field's message and code — never the value — instead of the 500 an uncaught ValueError would be. Mapped
    ONCE for the app, so a new route or a new service function cannot forget it. A privileged request leaves a
    best-effort audit line naming the route's path TEMPLATE (a path parameter is a request value too) and the
    FIELD; an open inquiry route (no principal) leaves none — a refusal must not give an unauthenticated caller a
    new way to write the audit log."""
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        route = request.scope.get("route")
        try:
            write_audit(request.app.state.settings.audit_log, principal.user, principal.ip, "request-refuse",
                        principal.run_id, "reason=request-boundary field=%s route=%s" % (
                            exc.field, getattr(route, "path", "-")))
        except OSError:
            pass                       # the refusal stands whether or not it could be recorded; nothing was written
    return JSONResponse(status_code=422, content={"detail": str(exc), "code": exc.code})


def _repository_refused(request: Request, exc: paths.RepositoryDataError) -> JSONResponse:
    """The repository's own data or layout stopped a request (SECURITY.md C22) — a module file whose adopted name
    is not one, a credential field declared badly, a backend with no role and a name none can be built from (or
    a registry role that is not a name), a write target outside the write root: answer **409** with a sentence
    that says where to look and `repository_data`, never 422 with a `code` naming a field the caller did not
    send, and never the 500 an uncaught exception would be. Mapped ONCE for the app. A privileged request leaves
    an audit line of its own ACTION (`repository-refuse what=<kind> route=<template>`), so a query for refused
    REQUESTS stays what it says; an open route (no principal) leaves none, as with a field refusal."""
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        route = request.scope.get("route")
        try:
            write_audit(request.app.state.settings.audit_log, principal.user, principal.ip, "repository-refuse",
                        principal.run_id, "what=%s route=%s" % (exc.what, getattr(route, "path", "-")))
        except OSError:
            pass                       # the refusal stands whether or not it could be recorded
    return JSONResponse(status_code=exc.status, content={"detail": str(exc), "code": exc.code})


def create_app(settings=None) -> FastAPI:
    """Build the FastAPI app. `settings` (defaults from the environment) carries the privileged-route
    Bearer token + audit-log path + the rate-limit policy; the read-only routes ignore the token. Route
    modules are registered explicitly in the loop below. The rate limiter is attached once here (a single
    middleware, no per-route edits — api/ratelimit.py)."""
    app = FastAPI(
        title="kontroll onboarding API",
        version="0.1.0",
        summary="Capability-aware onboarding inquiry over Ansible Galaxy + local content.",
        description="Inquiry (search / probe / classify / units) and token-gated routes — audited writes that "
                    "stage a proposal, and reads of the audit log and of field names — over the kontroll "
                    "service layer (docs/api-architecture.md). Never WAN-exposed.",
        lifespan=lifespan,
    )
    app.state.settings = settings or from_env()
    app.add_middleware(RateLimitMiddleware, settings=app.state.settings)
    app.add_exception_handler(paths.RequestBoundaryError, _boundary_refused)
    app.add_exception_handler(paths.RepositoryDataError, _repository_refused)
    for module in (health, search, probe, units, classify, refresh, exceptions, onboard, capability, secrets,
                   actuation, audit_routes):
        app.include_router(module.router)
    return app


app = create_app()
