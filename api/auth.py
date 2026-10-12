"""Bearer-token auth for the privileged routes (docs/api-architecture.md §4).

Fail-closed in two senses: if no token is configured (KONTROLL_API_TOKEN unset) every privileged
route is DISABLED (503) — the surface never allows an unauthenticated mutation; and a privileged
mutation that cannot be audited is REFUSED (503) rather than performed silently. A presented token
is compared constant-time (`hmac.compare_digest`); a bad/absent token is a 401 with a best-effort
`auth-denied` audit line (the token itself is never logged). Mirrors the GUI's fail-closed Basic
auth (gui/app.py), extended with Bearer tokens + a per-request run_id.
"""
import hmac

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.audit import mint_run_id, write_audit

_bearer = HTTPBearer(auto_error=False)


class Principal:
    """The authenticated caller for one request: a fixed user label, the client ip, and the run_id
    minted at the boundary — threaded into every audit line the request writes."""

    def __init__(self, user: str, ip: str, run_id: str):
        self.user = user
        self.ip = ip
        self.run_id = run_id


def _client_ip(request: Request) -> str:
    return request.headers.get("X-Forwarded-For", request.client.host if request.client else "-")


def require_token(request: Request,
                  creds: HTTPAuthorizationCredentials = Depends(_bearer)) -> Principal:
    """Authenticate a privileged request. No token configured ⇒ 503 (fail-closed; the surface is
    disabled). Constant-time compare; a bad/absent token ⇒ 401 + a best-effort `auth-denied` audit.
    On success returns the Principal carrying the request's run_id."""
    settings = request.app.state.settings
    ip, run_id = _client_ip(request), mint_run_id()
    if not settings.api_token:
        raise HTTPException(status_code=503,
                            detail="privileged API disabled: KONTROLL_API_TOKEN is not configured")
    if not (creds and hmac.compare_digest(creds.credentials, settings.api_token)):
        try:
            write_audit(settings.audit_log, "anonymous", ip, "auth-denied", run_id, request.url.path)
        except OSError:
            pass                       # denied-auth audit is best-effort; the 401 stands regardless
        raise HTTPException(status_code=401, detail="invalid or missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    principal = Principal(user="api-token", ip=ip, run_id=run_id)
    request.state.principal = principal        # for the app-wide boundary-refusal handler's audit line (api/main.py)
    return principal


def audit_action(request: Request, principal: Principal, action: str, detail: str = "") -> None:
    """Write a FAIL-CLOSED audit line for a privileged mutation — call it BEFORE mutating. If the
    write fails, the mutation must not proceed, so the failure becomes a 503 and the route returns
    before doing anything. (Use api.audit.write_audit directly for best-effort, non-blocking logs.)"""
    settings = request.app.state.settings
    try:
        write_audit(settings.audit_log, principal.user, principal.ip, action, principal.run_id, detail)
    except OSError as e:
        raise HTTPException(status_code=503, detail="audit log unavailable; mutation refused") from e
