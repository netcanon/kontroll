"""The FastAPI app factory.

`create_app()` builds the app; the lifespan loads the drop-in capability catalog
(vectors/overrides/backends) once into `app.state` (mirrors netcanon's one-time `create_app`
load). Routes are included as drop-in routers from `api/routes/` — adding a domain = adding a
file, never editing a hub (the same doctrine as inventory drop-ins, vectors, backends, services).

Scope (docs/api-architecture.md §8): read-only inquiry (search/probe/classify/health) + the
low-blast-radius privileged routes (POST /refresh, POST /capture-exceptions, GET /audit/log) behind
Bearer-token auth + a fail-closed, run_id-correlated audit. The highest-blast-radius mutation
(onboard --apply: repo write + canonical push over HTTP) is deliberately deferred. Privileged routes
are fail-closed: no KONTROLL_API_TOKEN ⇒ 503. Still **not deployed** — mgmt-VLAN-only, never WAN
(SECURITY.md C3/C9). Run locally for dev with: `uvicorn api.main:app --reload`.
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


def create_app(settings=None) -> FastAPI:
    """Build the FastAPI app. `settings` (defaults from the environment) carries the privileged-route
    Bearer token + audit-log path + the rate-limit policy; the read-only routes ignore the token. Each
    route group is a drop-in router — adding a domain = adding a file. The rate limiter is attached once
    here (a single middleware, no per-route edits — api/ratelimit.py)."""
    app = FastAPI(
        title="kontroll onboarding API",
        version="0.1.0",
        summary="Capability-aware onboarding inquiry over Ansible Galaxy + local content.",
        description="Read-only inquiry over the kontroll service layer (search / probe / classify). "
                    "Mutation routes, token auth, and audit land in later workstreams "
                    "(docs/api-architecture.md). Never WAN-exposed.",
        lifespan=lifespan,
    )
    app.state.settings = settings or from_env()
    app.add_middleware(RateLimitMiddleware, settings=app.state.settings)
    app.add_exception_handler(paths.RequestBoundaryError, _boundary_refused)
    for module in (health, search, probe, units, classify, refresh, exceptions, onboard, capability, secrets,
                   actuation, audit_routes):
        app.include_router(module.router)
    return app


app = create_app()
