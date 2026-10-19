"""openapi domain — derive an `api` backend recipe from an OpenAPI/Swagger spec.

Same prober -> classify -> overrides pattern as Galaxy onboarding, applied to a
machine-readable API spec. Honest limits (docs/capability-matrix.md §5.2): endpoint ->
capability is heuristic; the emitted recipe is STAGED — live-verify before trusting it.
fetch_spec / derive_auth / derive_server / classify_endpoints are pure(-ish) and tested
directly; build_openapi_recipe assembles the recipe + a summary the CLI prints. The
CLI owns the --emit write gate. Auth derivation emits VAR NAMES only, never values.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import yaml

from kontroll import paths

BACKUP_PATH_RE = re.compile(r"/(backup|export|running.?config|saveconfig|config/(backup|save))\b", re.I)
CHECK_PATH_RE = re.compile(r"/(status|health|healthz|ping|version|system(/status)?|api/v\d+/system)\b", re.I)


def fetch_spec(src):
    """Load an OpenAPI/Swagger spec from a URL or a local file (JSON or YAML)."""
    if src.startswith(("http://", "https://")):
        ctx = None
        if src.startswith("https://"):
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE       # self-hosted gear: self-signed is normal
        # a browser-ish UA: registries/CDNs (apis.guru) 403 the default Python-urllib one
        req = urllib.request.Request(src, headers={"User-Agent": "kontroll-galaxy/1.0", "Accept": "*/*"})
        try:
            raw = urllib.request.urlopen(req, timeout=20, context=ctx).read().decode()
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            sys.exit("could not fetch spec %s: %s (an API key may be needed, or use a local file)"
                     % (src, getattr(e, "reason", None) or e))
    else:
        raw = open(os.path.expanduser(src), encoding="utf-8").read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return yaml.safe_load(raw)


def derive_auth(spec, recipe_name):
    """securitySchemes (OpenAPI 3) / securityDefinitions (Swagger 2) -> auth block.
    Token/credential VARS are named (operator wires them to SOPS), never values."""
    schemes = ((spec.get("components") or {}).get("securitySchemes")
               or spec.get("securityDefinitions") or {})
    for _name, s in schemes.items():
        typ, scheme = (s.get("type") or "").lower(), (s.get("scheme") or "").lower()
        if typ == "http" and scheme == "bearer" or typ == "oauth2":
            return {"type": "bearer", "token_var": "%s_api_token" % recipe_name}
        if typ == "apikey" and (s.get("in") or "").lower() == "header":
            return {"type": "apikey_header", "header_name": s.get("name") or "X-Api-Key",
                    "token_var": "%s_api_token" % recipe_name}
        if typ == "http" and scheme == "basic" or typ == "basic":
            return {"type": "basic", "username_var": "%s_username" % recipe_name,
                    "password_var": "%s_password" % recipe_name}
    return {"type": "bearer", "token_var": "%s_api_token" % recipe_name,
            "_unverified": "no securityScheme in spec — defaulted to bearer; verify"}


def derive_server(spec):
    """-> (port, base_path) from servers[0] (v3) or host/basePath/schemes (v2)."""
    servers = spec.get("servers") or []
    if servers:
        u = urllib.parse.urlsplit(servers[0].get("url", ""))
        return (u.port or (443 if u.scheme != "http" else 80)), (u.path or "").rstrip("/")
    if spec.get("host"):
        u = urllib.parse.urlsplit("//" + spec["host"])
        https = "https" in (spec.get("schemes") or ["https"])
        return (u.port or (443 if https else 80)), (spec.get("basePath") or "").rstrip("/")
    return 443, ""


def classify_endpoints(spec, base_path):
    """Heuristic endpoint->capability. Returns the best backup + check GET endpoints
    and whether any write (actuate) endpoint exists."""
    backup, check, actuate = None, None, False
    for path, ops in (spec.get("paths") or {}).items():
        if not isinstance(ops, dict):
            continue
        full = base_path + path
        for method in ops:
            m = method.upper()
            if m in ("POST", "PUT", "PATCH", "DELETE"):
                actuate = True
            if m in ("GET", "POST") and BACKUP_PATH_RE.search(path):
                # prefer the shortest matching path (most canonical), GET over POST
                cand = {"method": m, "path": full, "extract": "body"}
                if backup is None or (m == "GET" and backup["method"] != "GET") \
                        or len(full) < len(backup["path"]):
                    backup = cand
            if m == "GET" and check is None and CHECK_PATH_RE.search(path):
                check = {"method": "GET", "path": full}
    return backup, check, actuate


def build_openapi_recipe(spec, name=None, port=None):
    """Derive an `api` backend recipe dict from a parsed OpenAPI/Swagger spec. Returns
    {"name","recipe","text","summary"} — summary carries the derived facts the CLI prints
    (title/version/port/base_path/auth/actuate/backup/check); text is the STAGED YAML to
    write. Heuristic (docs/capability-matrix.md §5.2): verify before relying on it."""
    info = spec.get("info") or {}
    if name:
        paths.component(name, "recipe name")     # it becomes a file name under the recipes directory
    name = name or re.sub(r"[^a-z0-9_]", "_", (info.get("title") or "api").lower())
    derived_port, base_path = derive_server(spec)
    if port:
        derived_port = port
    auth = derive_auth(spec, name)
    backup, check, actuate = classify_endpoints(spec, base_path)
    recipe = {"name": name,
              "description": "%s (derived from OpenAPI — verify before relying on it)" % (info.get("title") or name),
              "port": derived_port, "validate_certs": False,
              "auth": {k: v for k, v in auth.items() if not k.startswith("_")}}
    if check:
        recipe["check"] = check
    if backup:
        rb = dict(backup)
        rb["label"] = name
        rb["suffix"] = "config"
        recipe["backup"] = rb
    text = ("# Auto-derived from an OpenAPI spec by scripts/galaxy.py openapi.\n"
            "# STAGED — verify auth + the backup/check endpoints against a live instance\n"
            "# (the endpoint->capability mapping is heuristic) before relying on it.\n"
            + yaml.safe_dump(recipe, sort_keys=False, allow_unicode=True, width=4096))
    summary = {"title": info.get("title"), "version": info.get("version"),
               "port": derived_port, "base_path": base_path, "auth": auth,
               "actuate": actuate, "backup": backup, "check": check}
    return {"name": name, "recipe": recipe, "text": text, "summary": summary}
