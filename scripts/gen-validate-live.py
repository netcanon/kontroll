#!/usr/bin/env python3
"""scripts/gen-validate-live.py — the ONLINE half of the no-bespoke tenet's derive -> pin -> VALIDATE loop.

The offline `gen-* --check` scripts prove a pinned vendor fact is internally consistent (the generated artifact
matches its source). This is the twin that proves it still matches the LIVE device: it reaches each enabled class's
hosts read-only and confirms the pinned facts the consumers depend on haven't drifted on the wire — the TLS posture
(`vendor_defaults.tls_posture`, B2), the pulled API path (`logging/proxmox_api.yml source.endpoint`), the token's
read scope, and a device's SNMP OID support. Same "fail loud on drift" contract as the offline checks; it differs
ONLY in reaching the lab, so its tier is **control-VM / on-demand, NEVER hermetic CI** (it is deliberately absent
from tests/validate.sh — the dispatch + check logic is hermetically tested via an injected fake fetcher instead).

Shape (the proportionate design, isomorphic to gen-snmp's verdict/exit):
  * a top-level `validate:` sibling list on the descriptor that ALREADY owns the fact (no second copy of the fact):
    `modules/<key>/module.yml`, `logging/<m>.yml`, `telemetry/<m>.yml` — kept OUT of vendor_defaults/source/params,
    which other generators consume wholesale;
  * dispatched over the `_VALIDATORS` function table keyed by `check:` NAME — ZERO vendor branch (no function knows
    the word "proxmox"; the vendor-ness is descriptor DATA). A `tests/validate.sh` grep-gate keeps the spine literal-
    free and the transport read-only;
  * each check calls a mockable `LiveFetchers` (the ONLY device-reaching code — outbound GET / TLS handshake / SNMP
    GETNEXT, no write/POST primitive even exists), and returns OK | DRIFT | UNREACHABLE | PENDING-HARDENING:
    reach-then-judge (transport error = UNREACHABLE, tolerated; a successful mismatch = DRIFT, loud);
  * one `write_audit(action="validate-live", run_id, …)` line per check — the class/check/verdict NAMES, NEVER a
    credential (C12). The token is only an `${ENV}` ref resolved at the fetcher boundary.

Usage:
  python3 scripts/gen-validate-live.py                       # fan over the enabled fleet x inventory, all checks
  python3 scripts/gen-validate-live.py --class proxmox --target 192.0.2.12   # one class against one device
  python3 scripts/gen-validate-live.py --only snmp_oid_support --target 127.0.0.1   # one check (e.g. a smoke)
Exit: non-zero iff any check is DRIFT (UNREACHABLE/PENDING-HARDENING do not fail — a down host is tolerated, and a
device that has HARDENED past its pinned posture is a good change, not drift). See SECURITY.md C13.
"""
import argparse
import json
import os
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))   # `from kontroll import …`
sys.path.insert(0, ROOT)                             # `from api.audit import …`

from kontroll import catalog, endpoints, inventory, paths   # noqa: E402  (after sys.path)
from api.audit import mint_run_id, write_audit        # noqa: E402  (the run_id-correlated audit seam)

OK, DRIFT, UNREACHABLE, PENDING = "OK", "DRIFT", "UNREACHABLE", "PENDING-HARDENING"
_FAILING = {DRIFT}                                    # only DRIFT fails the run (the loud, actionable verdict)
_MISSING = object()                                   # "not present in the apidoc" sentinel (≠ a None permission)


def _audit_path():
    """The append-only TSV audit log: the validate-specific override, else the shared API audit log, else a
    local default. The same write_audit() the privileged API routes use (run_id-correlated, never a credential)."""
    return (os.environ.get("KONTROLL_VALIDATE_AUDIT_LOG") or os.environ.get("KONTROLL_API_AUDIT_LOG")
            or os.path.join(ROOT, "local", "validate-live-audit.log"))


class Verdict:
    """One check's outcome — the class/check/host it ran against, the verdict, and a human detail line. The detail
    carries NAMES only (class, check, secret-domain, status code), NEVER a credential value (C12 / M-5)."""
    def __init__(self, key, check, host, verdict, detail):
        self.key, self.check, self.host, self.verdict, self.detail = key, check, host, verdict, detail


class Target:
    """The class-level context a check runs against: the device address + the OWNING module's vendor TLS facts
    (so a logging/telemetry method, whose TLS posture is class-derived per B2's `tls_from_class`, resolves the
    same posture the consumer does — endpoints.tls_verify), and the module key for labelling/audit."""
    def __init__(self, key, host, vendor_defaults, cls_trust):
        self.key, self.host, self.vendor_defaults, self.cls_trust = key, host, vendor_defaults, cls_trust


# --- the device-reaching I/O boundary (the ONLY VM-only code; tests inject a fake) ----------------------------
class LiveFetchers:
    """Read-only device-reaching primitives. Every method is OUTBOUND read-only — a TLS handshake, an HTTP GET, an
    SNMP GETNEXT; there is deliberately NO write/POST/SET primitive here, so a check literally CANNOT actuate the
    device (the machine form of "read-only" — C13). Tests pass a fake with the same surface, so all dispatch +
    judgement logic is hermetic; this class is the sole code that must run on the control VM."""
    timeout = 8

    @staticmethod
    def _tls_context(ca_file=None, verify=True):
        """The ONE TLS context constructor for every outbound read: the verifying default context (or the unverified
        one the self-signed classes need — reachability, not trust), with the protocol floor pinned to TLS 1.2
        EXPLICITLY. That is the default on every supported Python, written down so an interpreter build or an
        OPENSSL_CONF cannot lower it, and so a reader (or CodeQL, py/insecure-protocol) does not have to know the
        default to know the floor. Every fetcher goes through here — test_gen_validate_live.py pins that no other
        line in this file builds a context."""
        ctx = ssl.create_default_context(cafile=ca_file) if verify else ssl._create_unverified_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    def tls_verifies(self, host, port, ca_file=None):
        """True iff host:port presents a cert chain that VERIFIES against `ca_file` (or the system trust store when
        None). Returns False on a certificate-verification failure (self-signed / wrong CA — a JUDGEMENT, not an
        outage). RAISES OSError on a transport failure (down / refused / timeout) -> the caller maps to UNREACHABLE.
        Read-only: a ClientHello + chain read; no request body is ever sent."""
        ctx = self._tls_context(ca_file=ca_file)
        try:
            with socket.create_connection((host, port), self.timeout) as sock:
                with ctx.wrap_socket(sock, server_hostname=host):
                    return True
        except ssl.SSLCertVerificationError:
            return False

    def http_get(self, url, headers=None, verify=True, ca_file=None):
        """GET `url` read-only -> {"status": int, "body": str}. `verify=False` uses an unverified context (the
        self-signed class default — we are confirming reachability/scope, not trusting the cert here); a pinned
        `ca_file` verifies against it. An HTTP error CODE (403/404) is a RESPONSE -> returned as status. A transport
        failure RAISES URLError/OSError -> UNREACHABLE. Method is hard-wired GET — no body, no other verb."""
        ctx = self._tls_context(ca_file=ca_file, verify=verify)
        req = urllib.request.Request(url, headers=headers or {}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                return {"status": resp.status, "body": resp.read().decode("utf-8", "replace")}
        except urllib.error.HTTPError as e:
            return {"status": e.code, "body": (e.read().decode("utf-8", "replace") if e.fp else "")}

    def snmp_getnext(self, host, oids, user, auth_pass, priv_pass, auth_proto="SHA", priv_proto="AES"):
        """SNMPv3 authPriv GETNEXT of `oids` on host:161 read-only via net-snmp's `snmpgetnext` -> {requested_oid:
        returned_oid}. RAISES OSError/CalledProcessError on a transport/timeout failure -> UNREACHABLE. VM-only
        (net-snmp present). The creds are passed as args only; they are NEVER logged or returned."""
        out = {}
        for oid in oids:
            proc = subprocess.run(
                ["snmpgetnext", "-v3", "-l", "authPriv", "-u", user, "-a", auth_proto, "-A", auth_pass,
                 "-x", priv_proto, "-X", priv_pass, "-On", "-t", "5", "-r", "1", "%s:161" % host, oid],
                capture_output=True, text=True, timeout=self.timeout + 4)
            if proc.returncode != 0:
                raise OSError("snmpgetnext rc=%d for %s" % (proc.returncode, oid))
            line = (proc.stdout or "").strip()
            out[oid] = line.split(" =", 1)[0].lstrip(".") if " =" in line else ""
        return out


# --- helpers --------------------------------------------------------------------------------------------------
def _fill_addr(endpoint, host):
    """The descriptor endpoint with the per-host address substituted (`${DEVICE_ADDR}` is what gen-logging fills)."""
    return endpoint.replace("${DEVICE_ADDR}", host)


def _split_pve_endpoint(endpoint):
    """A PVE REST endpoint -> (host_base, api_base, api_path). e.g. https://h:8006/api2/json/cluster/tasks ->
    ("https://h:8006", "https://h:8006/api2/json", "/cluster/tasks"). The api path + the /version corroboration
    + the node-local apidoc URL all derive from the ONE pinned endpoint — no second copy of the path."""
    marker = "/api2/json"
    host_base, _, rest = endpoint.partition(marker)
    return host_base, host_base + marker, rest or "/"


def _apidoc_endpoint_permissions(body, api_path, method):
    """Parse the PVE apidoc.js (`const apiSchema = [ … ];` — a JS file wrapping a JSON array) and return the
    `info.<method>.permissions` value for the node whose `path` == `api_path`, else `_MISSING`. Tolerant: a
    parse failure / a missing path or method returns `_MISSING` (the check maps that to UNREACHABLE — never a
    false DRIFT). `raw_decode` from the first `[` parses just the array and ignores the trailing JS."""
    try:
        schema, _ = json.JSONDecoder().raw_decode(body, body.index("["))
    except (ValueError, json.JSONDecodeError):
        return _MISSING
    stack = list(schema) if isinstance(schema, list) else []
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        if node.get("path") == api_path:
            return ((node.get("info") or {}).get(method) or {}).get("permissions", _MISSING)
        stack.extend(node.get("children") or [])
    return _MISSING


def _resolve_env_refs(template, environ):
    """Substitute every `${NAME}` in `template` from `environ` (the secret VALUE enters ONLY here, at the fetcher
    boundary). Raises KeyError if a referenced env var is unset -> the check maps that to UNREACHABLE (we cannot
    judge scope without the token; we never fall back to a privileged side-channel — M-4)."""
    out, i = [], 0
    while i < len(template):
        if template.startswith("${", i):
            j = template.index("}", i)
            out.append(environ[template[i + 2:j]])
            i = j + 1
        else:
            out.append(template[i])
            i += 1
    return "".join(out)


# --- the validators (dispatched by `check:` NAME; ZERO vendor branch) -----------------------------------------
def check_cert_posture(entry, ctx, target, fetchers):
    """Confirm the device's LIVE TLS cert still matches the class's pinned posture (B2, online).
    Access chain:   control VM -> TLS ClientHello to <host>:<port> -> read the served cert chain.
    May break:      nothing — a handshake; no request body is sent.
    Blast radius:   control node only (read-only handshake).
    Self_signed expected: a system-trust verify SHOULD fail -> OK; if it now SUCCEEDS the device has hardened to a
    publicly-trusted cert -> PENDING-HARDENING (a good change, not a cry-wolf DRIFT — M-9). CA pinned: the chain
    MUST verify against the pinned CA -> else DRIFT (the pin no longer matches the served cert)."""
    port = entry.get("port", 443)
    verify, ca_file = endpoints.tls_verify(target.vendor_defaults, target.cls_trust)
    try:
        verifies = fetchers.tls_verifies(target.host, port, ca_file if verify else None)
    except OSError as e:
        return Verdict(target.key, "cert_posture", target.host, UNREACHABLE,
                       "tls handshake failed (%s)" % type(e).__name__)
    if verify:
        return (Verdict(target.key, "cert_posture", target.host, OK, "pinned CA verifies the served cert")
                if verifies else
                Verdict(target.key, "cert_posture", target.host, DRIFT,
                        "pinned CA no longer verifies the served cert (cert rotated or CA changed)"))
    if verifies:
        return Verdict(target.key, "cert_posture", target.host, PENDING,
                       "device now serves a publicly-trusted cert (hardening) — consider pinning device_trust CA")
    return Verdict(target.key, "cert_posture", target.host, OK, "self-signed, matches pinned tls_posture")


def check_api_path(entry, ctx, target, fetchers):
    """Confirm the pinned PVE API path our pull depends on is still PUBLISHED in the device's own schema.
    Access chain:   control VM -> HTTPS GET <host>:8006/pve-docs/api-viewer/apidoc.js (node-local schema, mgmt-VLAN,
                    NO WAN egress; matches the version the device actually runs).
    May break:      nothing — a read-only GET of the device's served docs.
    Blast radius:   control node only.
    Catches PVE moving/removing the endpoint BEFORE the Vector pull silently 404s."""
    verify, ca_file = endpoints.tls_verify(target.vendor_defaults, target.cls_trust)
    host_base, _api_base, api_path = _split_pve_endpoint(_fill_addr(ctx["source"]["endpoint"], target.host))
    apidoc = host_base + "/pve-docs/api-viewer/apidoc.js"
    try:
        resp = fetchers.http_get(apidoc, verify=verify, ca_file=ca_file)
    except (urllib.error.URLError, OSError) as e:
        return Verdict(target.key, "api_path", target.host, UNREACHABLE, "apidoc GET failed (%s)" % type(e).__name__)
    if resp["status"] != 200:
        return Verdict(target.key, "api_path", target.host, UNREACHABLE,
                       "apidoc not served (HTTP %s) — cannot validate the schema here" % resp["status"])
    present = api_path in resp["body"]
    return (Verdict(target.key, "api_path", target.host, OK, "pinned path %s present in apidoc" % api_path)
            if present else
            Verdict(target.key, "api_path", target.host, DRIFT,
                    "pinned path %s is GONE from the published apidoc (endpoint moved/removed)" % api_path))


def check_schema_permission(entry, ctx, target, fetchers):
    """Confirm the device's published API schema still encodes the PERMISSION MODEL we assume for the pinned
    endpoint (A′, the schema-permission check). For `/cluster/tasks` that model is `{user: all}` — any
    authenticated token may call it, and access is enforced by PER-ROW audit FILTERING, not an endpoint
    permission. That is the exact assumption `token_scope`'s 200+empty `/version` disambiguation (M-8) rests on:
    if PVE TIGHTENS the endpoint to a hard `perm` check, a too-narrow token would start 403-ing and the model
    shifts. This catches that change from the SCHEMA (deterministic, hermetic-testable), before behaviour shifts
    on the wire. The pinned expectation is descriptor DATA (`expect:`); no role NAME is hardcoded (MF-4 — this
    check never references the grant literal). An optional `api_path:` on the entry pins the model of a DIFFERENT
    endpoint than the descriptor's pull — e.g. a Sys.Audit-GATED read the grant must cover
    (`{check: [perm, /, [Sys.Audit]]}` for /cluster/status) — generalizing this from the pull path to any endpoint
    the consumers depend on (the schema half of the role->privilege-map A′; grant_covers is its live half).
    Access chain:   control VM -> HTTPS GET <host>:8006/pve-docs/api-viewer/apidoc.js (node-local, no token, no WAN).
    May break:      nothing — a read-only GET of the device's served docs.
    Blast radius:   control node only."""
    verify, ca_file = endpoints.tls_verify(target.vendor_defaults, target.cls_trust)
    host_base, _api_base, derived = _split_pve_endpoint(_fill_addr(ctx["source"]["endpoint"], target.host))
    api_path = entry.get("api_path") or derived     # default = the pinned pull path; OVERRIDE pins another endpoint
    apidoc = host_base + "/pve-docs/api-viewer/apidoc.js"
    expect = entry.get("expect")
    try:
        resp = fetchers.http_get(apidoc, verify=verify, ca_file=ca_file)
    except (urllib.error.URLError, OSError) as e:
        return Verdict(target.key, "schema_permission", target.host, UNREACHABLE,
                       "apidoc GET failed (%s)" % type(e).__name__)
    if resp["status"] != 200:
        return Verdict(target.key, "schema_permission", target.host, UNREACHABLE,
                       "apidoc not served (HTTP %s) — cannot judge the schema here" % resp["status"])
    perms = _apidoc_endpoint_permissions(resp["body"], api_path, "GET")
    if perms is _MISSING:
        return Verdict(target.key, "schema_permission", target.host, UNREACHABLE,
                       "endpoint %s GET not found in apidoc (path drift is api_path's job)" % api_path)
    if perms == expect:
        return Verdict(target.key, "schema_permission", target.host, OK,
                       "permission model for %s unchanged: %s" % (api_path, json.dumps(expect)))
    return Verdict(target.key, "schema_permission", target.host, DRIFT,
                   "permission model for %s changed: pinned %s, live %s"
                   % (api_path, json.dumps(expect), json.dumps(perms)))


def check_token_scope(entry, ctx, target, fetchers):
    """Confirm the descriptor's own token can READ its pinned endpoint — the live backing for the grant
    prerequisite the onboard surface declares (the role NAME stays in the descriptor's provisioning block; this
    check is generic, judging only the HTTP status, so it never duplicates that literal — MF-4).
    Access chain:   control VM -> HTTPS GET <host>:8006<source.endpoint> with the descriptor's PVEAPIToken header.
    May break:      nothing — a read-only GET of the task history.
    Blast radius:   control node only.
    The token resolves from the descriptor's OWN secret_env_map env ref (never an admin side-channel — M-4); the
    value enters only at the fetcher boundary, never the audit/stdout (M-5). 200+data -> OK; 200+EMPTY is the
    audit-filter trap, disambiguated by a /version corroboration GET (M-8): /version 200 -> OK (token valid, no
    tasks), else DRIFT; 403 -> DRIFT (too narrow); transport -> UNREACHABLE."""
    verify, ca_file = endpoints.tls_verify(target.vendor_defaults, target.cls_trust)
    endpoint = _fill_addr(ctx["source"]["endpoint"], target.host)
    _host_base, api_base, _path = _split_pve_endpoint(endpoint)
    domain = ctx.get("secret_domain", "?")
    try:
        auth = _resolve_env_refs(ctx["source"]["auth"]["value"], os.environ)
    except KeyError:
        return Verdict(target.key, "token_scope", target.host, UNREACHABLE,
                       "token env for domain '%s' unset — cannot judge scope" % domain)
    headers = {"Authorization": auth}
    try:
        resp = fetchers.http_get(endpoint, headers=headers, verify=verify, ca_file=ca_file)
    except (urllib.error.URLError, OSError) as e:
        return Verdict(target.key, "token_scope", target.host, UNREACHABLE, "GET failed (%s)" % type(e).__name__)
    if resp["status"] == 403:
        return Verdict(target.key, "token_scope", target.host, DRIFT,
                       "token (domain '%s') is 403 on its pinned endpoint — scope too narrow" % domain)
    if resp["status"] != 200:
        return Verdict(target.key, "token_scope", target.host, UNREACHABLE,
                       "unexpected HTTP %s — cannot judge scope" % resp["status"])
    if _has_data(resp["body"]):
        return Verdict(target.key, "token_scope", target.host, OK, "token reads its endpoint (200, data)")
    # 200 + EMPTY: a too-narrow token gets an audit-FILTERED empty list, NOT a 403 — corroborate via /version.
    try:
        corr = fetchers.http_get(api_base + "/version", headers=headers, verify=verify, ca_file=ca_file)
    except (urllib.error.URLError, OSError) as e:
        return Verdict(target.key, "token_scope", target.host, UNREACHABLE,
                       "200+empty; /version corroboration failed (%s)" % type(e).__name__)
    if corr["status"] == 200:
        return Verdict(target.key, "token_scope", target.host, OK,
                       "token valid (200), endpoint empty — corroborated by /version")
    return Verdict(target.key, "token_scope", target.host, DRIFT,
                   "200+empty AND /version is %s — token (domain '%s') too narrow (not a benign empty)"
                   % (corr["status"], domain))


def check_snmp_oid_support(entry, ctx, target, fetchers):
    """Confirm the device answers SNMP GETNEXT for the pinned OID subtree (e.g. the snmp_exporter if_mib columns).
    Access chain:   control VM -> SNMPv3 authPriv GETNEXT to <host>:161 (udp), read-only.
    May break:      nothing — an SNMP GET reads counters; actuates NOTHING.
    Blast radius:   control node only.
    Creds resolve from the SNMPv3 env (set on the control VM from the snmp_observability SOPS domain), never logged.
    A returned OID still UNDER the requested subtree -> OK (supported); outside -> DRIFT (device dropped the MIB)."""
    user = os.environ.get("KONTROLL_SNMP_V3_USER")
    auth_pass = os.environ.get("KONTROLL_SNMP_V3_AUTH_PASS")
    priv_pass = os.environ.get("KONTROLL_SNMP_V3_PRIV_PASS")
    if not (user and auth_pass and priv_pass):
        return Verdict(target.key, "snmp_oid_support", target.host, UNREACHABLE,
                       "SNMPv3 creds not in env — cannot probe OID support")
    oids = entry.get("oids") or []
    try:
        got = fetchers.snmp_getnext(target.host, oids, user, auth_pass, priv_pass)
    except (OSError, subprocess.SubprocessError) as e:
        return Verdict(target.key, "snmp_oid_support", target.host, UNREACHABLE,
                       "snmp getnext failed (%s)" % type(e).__name__)
    unsupported = [o for o in oids if not (got.get(o) or "").startswith(o)]
    return (Verdict(target.key, "snmp_oid_support", target.host, OK, "OID subtree(s) supported: %s" % ",".join(oids))
            if not unsupported else
            Verdict(target.key, "snmp_oid_support", target.host, DRIFT,
                    "device does not answer under pinned OID(s) %s (MIB support dropped)" % ",".join(unsupported)))


def check_grant_covers(entry, ctx, target, fetchers):
    """Confirm the descriptor's own token COVERS a permission-GATED endpoint — the live backing for the onboard
    provisioning grant (PR #19's `provisioning:` instructs the operator to grant a role that confers the required
    permission; this proves that grant actually reaches a gated read). The complement to token_scope: token_scope
    exercises the {user: all} pull (ANY authenticated token reads it, audit-row-FILTERED — hence its 200+empty
    /version dance), whereas this exercises a HARD perm-gated endpoint (`entry.api_path`, e.g. /cluster/status ->
    `{check: [perm, /, [Sys.Audit]]}`), where insufficient privilege returns an UNAMBIGUOUS 403. So a 200 proves
    the grant confers the gate's permission; a 403 means it no longer does and the privileged reads depending on it
    (the proxmox metrics need Sys.Audit) will break. The required permission is descriptor DATA (`perm:`, used only
    in the message); the check judges ONLY the HTTP status, never hardcoding a role/permission literal (MF-4).
    Access chain:   control VM -> HTTPS GET <host>:8006/api2/json<api_path> with the descriptor's PVEAPIToken.
    May break:      nothing — a read-only GET of a privileged read endpoint.
    Blast radius:   control node only.
    The token resolves from the descriptor's OWN secret_env_map env ref (never an admin side-channel — M-4); the
    value enters only at the fetcher boundary, never the audit/stdout (M-5)."""
    verify, ca_file = endpoints.tls_verify(target.vendor_defaults, target.cls_trust)
    _host_base, api_base, _path = _split_pve_endpoint(_fill_addr(ctx["source"]["endpoint"], target.host))
    api_path = entry.get("api_path") or "/"
    perm = entry.get("perm", "the gate's permission")
    domain = ctx.get("secret_domain", "?")
    try:
        auth = _resolve_env_refs(ctx["source"]["auth"]["value"], os.environ)
    except KeyError:
        return Verdict(target.key, "grant_covers", target.host, UNREACHABLE,
                       "token env for domain '%s' unset — cannot judge grant coverage" % domain)
    try:
        resp = fetchers.http_get(api_base + api_path, headers={"Authorization": auth}, verify=verify, ca_file=ca_file)
    except (urllib.error.URLError, OSError) as e:
        return Verdict(target.key, "grant_covers", target.host, UNREACHABLE, "GET failed (%s)" % type(e).__name__)
    if resp["status"] == 200:
        return Verdict(target.key, "grant_covers", target.host, OK,
                       "grant covers %s (200) — token confers %s" % (api_path, perm))
    if resp["status"] == 403:
        return Verdict(target.key, "grant_covers", target.host, DRIFT,
                       "token (domain '%s') is 403 on the gated %s — grant no longer confers %s (reads will break)"
                       % (domain, api_path, perm))
    return Verdict(target.key, "grant_covers", target.host, UNREACHABLE,
                   "unexpected HTTP %s on %s — cannot judge grant coverage" % (resp["status"], api_path))


_VALIDATORS = {"cert_posture": check_cert_posture, "api_path": check_api_path,
               "schema_permission": check_schema_permission,
               "token_scope": check_token_scope, "grant_covers": check_grant_covers,
               "snmp_oid_support": check_snmp_oid_support}


def _has_data(body):
    """A PVE list response is {"data": [...]} — True iff it parsed to a non-empty data array."""
    try:
        doc = yaml.safe_load(body) or {}           # JSON is a subset of YAML; avoids an extra import
    except yaml.YAMLError:
        return False
    return bool(isinstance(doc, dict) and doc.get("data"))


# --- the fan (enabled modules x their validate: blocks x inventory hosts) --------------------------------------
def _load(rel):
    with open(rel, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _load_optional(rel):
    p = paths.resolve(rel)
    return _load(p) if os.path.exists(p) else {}


def _hosts_in_group(inv, group):
    """{hostname: ansible_host} for a leaf inventory group (the gen-observability shape — M-7)."""
    node = ((inv.get("all") or {}).get("children") or {}).get(group) or {}
    return {name: (h or {}).get("ansible_host")
            for name, h in (node.get("hosts") or {}).items() if (h or {}).get("ansible_host")}


def _pairs_for_module(module, logging_by, telem_by):
    """The (validate-entry, owning-descriptor) pairs for a module: its OWN top-level `validate:`, plus the
    top-level `validate:` of each logging/telemetry method it references (`logs:`/`metrics:`). A UNIFORM top-level
    read across all three descriptor families — the `validate:` sibling key is deliberately NOT nested inside
    vendor_defaults/source/params (which other generators consume wholesale). Knows only descriptor SHAPES (logs/
    metrics references), NEVER a vendor literal — the dispatch stays vendor-blind (M-3)."""
    pairs = [(e, module) for e in (module.get("validate") or [])]
    for ref in (module.get("logs") or []):
        m = logging_by.get(ref.get("method"))
        if m:
            pairs += [(e, m) for e in (m.get("validate") or [])]
    for ref in (module.get("metrics") or []):
        m = telem_by.get(ref.get("method"))
        if m:
            pairs += [(e, m) for e in (m.get("validate") or [])]
    return pairs


def collect(fleet, inv, only_key=None, target_host=None, logging_by=None, telem_by=None, instance_trust=None):
    """Yield (entry, ctx, Target) work items. `only_key` restricts to one module; `target_host` overrides the
    inventory fan with a single on-demand address (the per-device / smoke mode). Fail-CLOSED on an unknown check
    is enforced at dispatch, not here."""
    logging_by = logging_by if logging_by is not None else {m["name"]: m for m in catalog.load_logging()}
    telem_by = telem_by if telem_by is not None else {m["name"]: m for m in catalog.load_telemetry()}
    instance_trust = instance_trust if instance_trust is not None else (
        _load_optional("instance/instance.yml").get("device_trust") or {})
    for key in paths.enabled_keys(fleet):
        if only_key and key != only_key:
            continue
        mp = os.path.join(paths.MODULES_DIR, key, "module.yml")
        if not os.path.exists(mp):
            continue
        module = _load(mp)
        vd = module.get("vendor_defaults") or {}
        cls_trust = instance_trust.get(key)
        hosts = [target_host] if target_host else sorted(_hosts_in_group(inv, module.get("inventory_group")).values())
        pairs = _pairs_for_module(module, logging_by, telem_by)
        for host in hosts:
            for entry, ctx in pairs:
                yield entry, ctx, Target(key, host, vd, cls_trust)


def run(only_key=None, target_host=None, only_check=None, fetchers=None, audit_path=None):
    """Run the collected validations and return (verdicts, exit_code). Each check dispatches by `check:` NAME over
    _VALIDATORS (fail-CLOSED on an unknown name) and emits one run_id-correlated audit line (NAMES only)."""
    fetchers = fetchers or LiveFetchers()
    audit_path = audit_path or _audit_path()
    fleet = _load(paths.resolve("config/fleet.yml"))
    for problem in paths.fleet_problems(fleet):              # skipped, and said: the position, never the entry
        sys.stderr.write("gen-validate-live: ignoring: instance/fleet.yml: %s\n" % problem)
    inv = inventory.merged_inventory(paths.resolve("ansible/inventory/hosts.yml"))
    run_id = mint_run_id()
    verdicts = []
    for entry, ctx, target in collect(fleet, inv, only_key=only_key, target_host=target_host):
        check = entry.get("check")
        if only_check and check != only_check:
            continue
        fn = _VALIDATORS.get(check)
        if fn is None:
            sys.exit("gen-validate-live: unknown check %r on %s — descriptor typo (fail-closed)"
                     % (check, target.key))
        v = fn(entry, ctx, target, fetchers)
        verdicts.append(v)
        write_audit(audit_path, "validate-cli", "local", "validate-live", run_id,
                    "%s %s %s" % (v.key, v.check, v.verdict))     # NAMES only — never the token (M-5/C12)
    return verdicts, (1 if any(v.verdict in _FAILING for v in verdicts) else 0)


def _print(verdicts):
    if not verdicts:
        print("gen-validate-live: no validate: blocks matched (nothing to check)")
        return
    width = max(len(v.key) for v in verdicts)
    for v in verdicts:
        print("%-11s %-*s %-18s %s" % (v.verdict, width, v.key, v.check, v.detail))
    n_drift = sum(v.verdict == DRIFT for v in verdicts)
    n_unreach = sum(v.verdict == UNREACHABLE for v in verdicts)
    print("-- %d checks: %d OK/PENDING, %d UNREACHABLE (tolerated), %d DRIFT"
          % (len(verdicts), len(verdicts) - n_drift - n_unreach, n_unreach, n_drift))


def main(argv):
    ap = argparse.ArgumentParser(description="Online drift validation of pinned vendor facts (read-only).")
    ap.add_argument("--class", dest="cls", help="restrict to one module key")
    ap.add_argument("--target", help="override the inventory fan with a single device address (on-demand/smoke)")
    ap.add_argument("--only", help="restrict to one check name (e.g. token_scope)")
    args = ap.parse_args(argv)
    verdicts, code = run(only_key=args.cls, target_host=args.target, only_check=args.only)
    _print(verdicts)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
