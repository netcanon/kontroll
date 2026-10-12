#!/usr/bin/env python3
"""scripts/kontroll-discover.py — the PASSIVE-DISCOVERY sweep actor (discovery-inbox Rung 1a, the read spine).

Reaches each already-onboarded device-class that OFFERS a discovery method and reads its OWN lease/neighbour view
read-only, so the un-onboarded hosts on the network can be surfaced into an onboard *inbox*. It writes the
git-ignored `local/discovery-inbox.generated.json`; the GUI reads that artifact through the PURE
`scripts/kontroll/service/discovery.read_inbox()` (which never reaches a device or writes a byte). A human still
onboards + promotes every surfaced host — discovery only pre-fills the existing onboard form (C10 unchanged; there
is NO auto-onboard).

WHY this is a TOP-LEVEL actor (outside `scripts/kontroll/service/`, like `kontroll-autopromoter.py` sits outside
the read-only `_pinned_layer`): the sweep WRITES the cached artifact, and an `open(cache, 'w')` inside a
`service/*.py` module would trip the read-only completeness gate (`tests/unit/_readonly_pins.py`). Keeping the
device-reaching read + the artifact write HERE is exactly what lets `service/discovery.read_inbox` stay provably
read-only — the zero-new-authority proof this feature rests on (design-of-record §"the OUT-OF-BAND cached model").

Passive + read-only by CONSTRUCTION (SECURITY C18):
  * the fetcher is a single hard-wired `http_get` (an HTTP GET), byte-capped at the socket — there is NO
    write/POST/SET/scan primitive, and no CIDR expansion: the sweep reaches ONLY an onboarded device's own address.
    A `tests/validate.sh` `discovery-passive-only` grep-gate keeps a scanning/write transport un-expressible.
  * parse dispatches over the `_PARSERS` table keyed by the descriptor's `parse.shape` NAME — ZERO vendor branch (no
    function knows the word "opnsense"; the vendor-ness is descriptor DATA), the no-bespoke-config tenet.
  * the response is UNTRUSTED DATA: byte-capped at the socket + row-count-capped at parse (flood cap) before it is
    written; every field is re-normalized on the pure READ side (`service/discovery`) before it can reach the GUI.
  * one `write_audit(action="discover", ...)` line per source — the class/method/status/count NAMES only, NEVER a
    credential and NEVER a discovered identifier (C12).

Tier: control-VM / on-demand, NEVER hermetic CI or an install-gating path (it reaches the lab — the BRICK-1
discipline; deliberately absent from `tests/validate.sh` + bootstrap). Its dispatch/parse is proven hermetically via
an injected fake fetcher (`tests/unit/test_discovery.py`). Usage:
  python3 scripts/kontroll-discover.py                       # sweep the enabled fleet's offered discovery methods
  python3 scripts/kontroll-discover.py --class opnsense      # restrict to one class
"""
import argparse
import base64
import ipaddress
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))   # `from kontroll import ...`
sys.path.insert(0, ROOT)                             # `from api.audit import ...`

from kontroll import catalog, paths                        # noqa: E402  (after sys.path)
from kontroll.service import discovery as discovery_svc     # noqa: E402  (inbox_path — the ONE path source of truth)
from kontroll.service import fleet as fleet_svc             # noqa: E402  (the onboarded-inventory read, reused)
from api.audit import mint_run_id, write_audit              # noqa: E402  (the run_id-correlated audit seam)

SCHEMA = 1
MAX_ROWS = 4096            # flood cap: rows kept per source (count-capped at parse, BEFORE the artifact is written)
MAX_BYTES = 4 * 1024 * 1024   # flood cap: bytes read per response at the socket (a hostile device can't OOM the sweep)
MAX_WALK_LINES = 8192      # flood cap: net-snmp walk lines kept per source (one varbind/line; caps a giant ARP table)


def _audit_path():
    """The append-only TSV audit log: the discover-specific override, else the shared API audit log, else a
    write_root-relative default. The same write_audit() the validate seam + the privileged API routes use
    (run_id-correlated, NAMES). The default is under `paths.write_root()` — NOT `ROOT` — mirroring `inbox_path()`:
    on a Phase-B BAKED deploy ROOT is the read-only `/opt/kontroll` image tree, so a `ROOT/local` audit write raises
    PermissionError and aborts the whole sweep; write_root() is the writable propose clone (`==ROOT` off a baked
    deploy, so this is byte-identical there). Live-caught on the baked-box discovery dogfood 2026-07-03."""
    return (os.environ.get("KONTROLL_DISCOVER_AUDIT_LOG") or os.environ.get("KONTROLL_API_AUDIT_LOG")
            or os.path.join(paths.write_root(), "local", "discover-audit.log"))


# --- the device-reaching I/O boundary (the ONLY VM-only code; tests inject a fake) ----------------------------
class _Fetchers:
    """Read-only device-reaching primitive for discovery — a single hard-wired HTTP GET, byte-capped at the socket
    (the flood cap). There is deliberately NO write/POST/SET/scan primitive, so the sweep literally CANNOT actuate
    or scan (the machine form of "passive read-only" — C18). Cloned from gen-validate-live.LiveFetchers.http_get
    (minus the SNMP/TLS-probe surface, plus the byte cap). Tests pass a fake with the same 1-method surface, so all
    dispatch + parse logic is hermetic; this class is the sole code that must run on the control VM."""
    timeout = 8

    def http_get(self, url, headers=None, verify=False, ca_file=None):
        """GET `url` read-only -> {"status": int, "body": str}, reading at most MAX_BYTES. `verify=False` uses an
        unverified context (a self-signed firewall API; we are reading leases, not trusting the cert). An HTTP error
        CODE (401/403/404) is a RESPONSE -> returned as status. A transport failure RAISES URLError/OSError -> the
        caller records the source `unreachable`. Method is hard-wired GET — no body, no other verb."""
        ctx = ssl.create_default_context(cafile=ca_file) if verify else ssl._create_unverified_context()
        req = urllib.request.Request(url, headers=headers or {}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                return {"status": resp.status, "body": resp.read(MAX_BYTES).decode("utf-8", "replace")}
        except urllib.error.HTTPError as e:
            return {"status": e.code, "body": (e.read(MAX_BYTES).decode("utf-8", "replace") if e.fp else "")}

    def snmp_walk(self, host, oid, user, auth_pass, priv_pass, auth_proto="SHA", priv_proto="AES"):
        """Read-only SNMPv3 authPriv WALK of `oid` on host:161 via net-snmp's `snmpbulkwalk` -> a list of raw varbind
        lines (`<numeric-oid> = <TYPE>: <value>`). READ-ONLY by construction: the verb is the code literal
        "snmpbulkwalk" (GETBULK only) — there is NO write-PDU (SNMP SET) path and no descriptor key selects a verb.
        STREAMED + killed-on-overflow (the S-CAP flood bound): a hostile/large ARP table CANNOT make the parent buffer
        more than the cap — we read at most MAX_BYTES+1 from the child pipe and KILL the child on overflow (the SNMP
        analogue of the HTTP `resp.read(MAX_BYTES)`; `capture_output=True` would buffer the whole child stdout first).
        Creds are list argv (never shell-interpolated — no shell string) and NEVER appear in a raised message / audit /
        stdout: stderr is discarded and a nonzero rc raises a CRED-FREE `rc=<n>` OSError (the argv-in-process-table
        residual of net-snmp's -A/-X is accepted under C3, the single-operator mgmt VLAN). RAISES OSError on a nonzero
        rc with no output -> the caller records `unreachable`. VM-only (net-snmp present; a missing binary is a
        FileNotFoundError -> unreachable); tests inject a fake with the same list-returning surface, so all dispatch +
        parse is hermetic. NOTE: `-Cr50` GETBULK max-repetitions + `-t 5 -r 1` bound each request; the byte/line caps
        bound the whole."""
        argv = ["snmpbulkwalk", "-v3", "-l", "authPriv", "-u", user, "-a", auth_proto, "-A", auth_pass,
                "-x", priv_proto, "-X", priv_pass, "-On", "-Oe", "-OU", "-Cr50",
                "-t", "5", "-r", "1", "%s:161" % host, oid]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            data = proc.stdout.read(MAX_BYTES + 1)          # bounded PARENT read: at most one byte past the cap
        finally:
            if proc.poll() is None:
                proc.kill()                                  # a device streaming forever is killed -> memory bounded
            try:
                proc.wait(timeout=self.timeout)              # reap; never leave a zombie net-snmp
            except subprocess.TimeoutExpired:
                proc.kill()
        if proc.returncode not in (0, None) and not data:    # nonzero rc + no output = down/auth-fail -> unreachable
            raise OSError("snmpbulkwalk rc=%s" % proc.returncode)   # CRED-FREE (never user/pass/argv/stderr)
        text = data[:MAX_BYTES].decode("utf-8", "replace")   # S-UTF8: never raise on non-UTF8 device bytes
        return text.splitlines()[:MAX_WALK_LINES]

    # AUTHORSHIP NOTE (the discovery-passive-only gate greps this WHOLE file case-sensitively): a capitalized
    # ssh-option word, a file-transfer/tunnel/option-indirection/alternate-spawn primitive named literally anywhere
    # here — including in a docstring — is a DELIBERATE fail-closed trip (C18: this actor may express only a read-only
    # forced-command read). Keep this fetcher's prose lowercase and name no such primitive literally. See
    # tests/validate.sh `discovery-passive-only` + test_ssh_read_is_forced_command_read_only (the argv-AST twin).
    def ssh_read(self, host, user, keyfile, known_hosts):
        """Read a device file over SSH via a READ-ONLY, forced-command key -> the file text, reading at most MAX_BYTES.
        READ-ONLY by CONSTRUCTION: NO client command is sent — `ssh` is invoked with the command word ABSENT (the argv
        ends at the `<user>@<host>` token), so the device's authorized-key forced command (a single fixed read — e.g.
        reading one lease file, no tty, no shell) is the ONLY thing that can run; the sweep cannot choose what runs. NO
        host-key TOFU: StrictHostKeyChecking=yes + an operator-PINNED UserKnownHostsFile (a changed/unknown host key
        REFUSES the connection -> OSError -> the caller records `unreachable`, never a silent MITM-accept). BatchMode=yes
        + IdentitiesOnly=yes: never prompt, never fall back to a password or an agent key. STREAMED + killed-on-overflow
        (the S-CAP flood bound, identical to snmp_walk): a hostile/huge file cannot OOM the sweep — read at most
        MAX_BYTES+1 from the child pipe and KILL the child on overflow. Paths are list argv (never shell-interpolated —
        the argv is a list, not a shell string) and NEVER appear in a raised message / audit / stdout: stderr is
        discarded, a nonzero rc raises a CRED-FREE `rc=<n>` OSError. VM-only (the ssh client is present; a missing binary
        is FileNotFoundError -> the caller records `unreachable`); tests inject a fake with the same 1-method surface, so
        all dispatch + parse is hermetic."""
        argv = ["ssh", "-i", keyfile,
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes",
                "-o", "UserKnownHostsFile=%s" % known_hosts,
                "-o", "IdentitiesOnly=yes",
                "-o", "ConnectTimeout=%d" % self.timeout,
                "%s@%s" % (user, host)]          # NO client command word — the device's forced command is all that runs
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            data = proc.stdout.read(MAX_BYTES + 1)          # bounded PARENT read: at most one byte past the cap
        finally:
            if proc.poll() is None:
                proc.kill()                                  # a device streaming forever is killed -> memory bounded
            try:
                proc.wait(timeout=self.timeout)              # reap; never leave a zombie ssh
            except subprocess.TimeoutExpired:
                proc.kill()
        if proc.returncode not in (0, None) and not data:    # nonzero rc + no output = down/auth-fail -> unreachable
            raise OSError("ssh rc=%s" % proc.returncode)     # CRED-FREE (never key path / user / host / argv / stderr)
        return data[:MAX_BYTES].decode("utf-8", "replace")   # S-UTF8: never raise on non-UTF8 device bytes


def _resolve_env_refs(template, environ):
    """Substitute every `${NAME}` in `template` from `environ` (the secret VALUE enters ONLY here, at the fetcher
    boundary). Raises KeyError if a referenced env var is unset -> the caller records the source `no-creds` (we
    cannot read without the token; we never fall back to a privileged side-channel). Lifted from gen-validate-live."""
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


def _auth_headers(auth, environ):
    """The Authorization header for a source's `auth` block, resolving its `${ENV}` refs at the boundary (may raise
    KeyError when a cred env is unset). `basic` => `Authorization: Basic base64(user:pass)`; `custom` => the value
    verbatim; anything else / absent => no header. The secret value never leaves this dict (never audited/artifacted)."""
    if not auth:
        return {}
    value = _resolve_env_refs(auth.get("value") or "", environ)
    strategy = auth.get("strategy")
    if strategy == "basic":
        token = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return {"Authorization": "Basic %s" % token}
    if strategy == "custom":
        return {"Authorization": value}
    return {}


# --- the parsers (dispatched by `parse.shape` NAME; ZERO vendor branch) ----------------------------------------
def _parse_json_rows(body, parse):
    """A JSON body `{<rows>: [ {<fields>}, ... ]}` (or a bare top-level array) -> a list of {ip, mac, hostname}
    dicts, mapping each canonical key from the response field NAME the descriptor's `parse.fields` gives. Knows NO
    vendor: the array field + the column names are descriptor DATA. A malformed body / non-list rows -> [] (never
    raises — a hostile device blanks its own source, never the sweep). Values are passed through RAW here; the pure
    READ side (`service/discovery`) is what validates/normalizes them before they reach the GUI."""
    try:
        doc = json.loads(body)
    except (ValueError, TypeError):
        return []
    rows_field = parse.get("rows")
    if rows_field and isinstance(doc, dict):
        rows = doc.get(rows_field)
    elif isinstance(doc, list):
        rows = doc
    else:
        rows = []
    fields = parse.get("fields") or {}
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        out.append({"ip": r.get(fields.get("ip")),
                    "mac": r.get(fields.get("mac")),
                    "hostname": r.get(fields.get("hostname"))})
    return out


# A 6-octet MAC in space/colon/dash-separated hex (net-snmp renders a MAC OCTET STRING as `Hex-STRING: 00 00 5E ...`).
# {1,2} hex per octet, NOT {2}: net-snmp renders ipNetToMediaPhysAddress WITHOUT the IP-MIB PhysAddress DISPLAY-HINT
# (the runtime image ships net-snmp-tools, not the MIB files) as a colon `STRING:` with leading zeros COLLAPSED — a
# 0x00/0x0a octet prints as `0`/`a`, e.g. `0:0:5e:0:53:1`. Each captured octet is zero-padded to 2 digits below.
_MAC_OCTETS = re.compile(r"^([0-9A-Fa-f]{1,2})[ :-]([0-9A-Fa-f]{1,2})[ :-]([0-9A-Fa-f]{1,2})[ :-]"
                         r"([0-9A-Fa-f]{1,2})[ :-]([0-9A-Fa-f]{1,2})[ :-]([0-9A-Fa-f]{1,2})$")
# Well-known NON-identifier MACs (an incomplete/expired ARP entry) — dropped as noise, never surfaced as a candidate.
_NULL_MACS = {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"}


def _hexmac_to_colon(value):
    """A net-snmp MAC value (`Hex-STRING: 00 00 5E 00 53 01`, the collapsed colon `STRING: 0:0:5e:0:53:1`, or a
    bare colon/dash/space form) -> a canonical zero-padded lowercase colon MAC, or None for anything that is not
    EXACTLY 6 space/colon/dash-separated hex octets (junk -> None; the read side keeps the IP and degrades the mac).
    Each octet is zero-padded to 2 digits so the DISPLAY-HINT-less `STRING:` form (leading zeros collapsed, e.g.
    `0:0:5e:0:53:1`) normalizes to `00:00:5e:00:53:01` — live-caught on the cisco SNMP-ARP dogfood, a real switch
    neighbour with a sub-0x10 octet was dropped. Strips a leading `<TYPE>: ` net-snmp type prefix."""
    val = value.split(": ", 1)[1] if ": " in value else value
    m = _MAC_OCTETS.match(val.strip())
    return ":".join(g.lower().zfill(2) for g in m.groups()) if m else None


def _parse_snmp_arp_table(lines, parse):
    """SNMP ipNetToMediaTable walk lines (net-snmp `<numeric-oid> = <TYPE>: <value>`) -> [{ip, mac, hostname:None}].
    The walked ipNetToMediaPhysAddress column's row INDEX is `<ifIndex>.<a.b.c.d>`, so the IPv4 is the LAST 4
    dot-separated sub-identifiers of every instance OID (the parser reads NO OID from the descriptor — the
    last-4-octets rule is intrinsic to an IPv4 ARP table) and the MAC is the value. Vendor-blind + hostile-tolerant
    like _parse_json_rows: a line with no ` = `, a `No Such`/`End of MIB` value, an OID with <4 sub-ids, or an
    all-zero/broadcast MAC (an incomplete/expired ARP entry) is SKIPPED; a non-MAC value degrades mac->None but keeps
    the IP; a malformed line NEVER raises. Assumes ONE varbind per line (a 6-octet MAC never wraps; a future wide
    table would need a continuation join). Values are RAW here; the pure READ side (service/discovery) re-validates the
    ip/mac before the GUI. `parse` is accepted for signature-parity with the _PARSERS table (this shape reads none)."""
    out = []
    for line in lines if isinstance(lines, list) else []:
        if not isinstance(line, str) or " = " not in line:
            continue
        oid, _, value = line.strip().partition(" = ")
        if value.lstrip().startswith(("No Such", "End of MIB")):    # a per-instance MIB error -> not a real neighbour
            continue
        subids = oid.lstrip(".").split(".")
        if len(subids) < 4:
            continue
        mac = _hexmac_to_colon(value)
        if mac in _NULL_MACS:                                       # incomplete/expired ARP slot -> noise, skip
            continue
        out.append({"ip": ".".join(subids[-4:]), "mac": mac, "hostname": None})
    return out


def _index_to_ip(subids):
    """Decode an ipNetToPhysical row-index tail (`…<ifIndex>.<addrType>.<len>.<addr-byte>…`, RFC-4293) -> a v4 OR v6
    STRING, or None for anything that is not a plain ipv4/ipv6 InetAddress. Scan from the RIGHT so a leading ifIndex of
    ANY width is irrelevant: the address is the trailing `len` sub-ids, preceded by `<addrType>.<len>` — addrType 1=ipv4
    (len 4), 2=ipv6 (len 16). A type we don't handle (ipv4z/ipv6z/dns/unknown), a length that doesn't match its type,
    an out-of-octet-range sub-id, or too few sub-ids -> None (the caller then skips the row — hostile-tolerant, never
    raises). Builds the string from raw bytes via the pure `ipaddress` address constructors (NO name lookup, NO socket,
    NO CIDR-expansion constructor) so the canonical form is exact (`2001:db8::1`, never a mis-zero-padded hand-join)."""
    if len(subids) < 3:                                # need at least addrType + len + 1 address byte
        return None
    try:
        ints = [int(s) for s in subids]
    except ValueError:
        return None
    if any(b < 0 or b > 255 for b in ints):           # a sub-id out of octet range -> not the byte-run we decode
        return None
    for addr_type, length, builder in ((1, 4, ipaddress.IPv4Address), (2, 16, ipaddress.IPv6Address)):
        if len(ints) < length + 2:
            continue
        head, tail = ints[:-length], ints[-length:]
        if head[-2:] == [addr_type, length]:          # `…<addrType>.<len>.<`length` address bytes>`
            try:
                return str(builder(bytes(tail)))
            except (ValueError, ipaddress.AddressValueError):
                return None
    return None


def _parse_snmp_neighbors_table(lines, parse):
    """SNMP ipNetToPhysicalTable walk lines (net-snmp `<numeric-oid> = <TYPE>: <value>`, RFC-4293) -> [{ip, mac,
    hostname:None}] for BOTH v4 and v6 neighbours. The walked ipNetToPhysicalPhysAddress column's row INDEX is
    `<ifIndex>.<addrType>.<len>.<addr-bytes>` (a TYPE-tagged, LENGTH-prefixed InetAddress — NOT the last-4 rule the v4
    `arp_table` parser uses), so the IP is decoded via `_index_to_ip` (v4 dotted-quad OR v6 colon-hex) and the MAC is
    the value (the same `_hexmac_to_colon` + null-MAC drop as arp_table). Vendor-blind + hostile-tolerant: a line with
    no ` = `, a `No Such`/`End of MIB` value, an index that doesn't decode to a plain ipv4/ipv6 address (ipv4z/ipv6z/
    dns/unknown types, a wrong length, an out-of-range byte, too few sub-ids), or an all-zero/broadcast MAC is SKIPPED;
    a non-MAC value degrades mac->None but keeps the IP; a malformed line NEVER raises. Assumes ONE varbind per line.
    Values are RAW here; the pure READ side (service/discovery) re-validates ip/mac before the GUI (its `_clean_ip` is
    dual-stack). `parse` is accepted for _PARSERS signature-parity (this shape reads none)."""
    out = []
    for line in lines if isinstance(lines, list) else []:
        if not isinstance(line, str) or " = " not in line:
            continue
        oid, _, value = line.strip().partition(" = ")
        if value.lstrip().startswith(("No Such", "End of MIB")):
            continue
        ip = _index_to_ip(oid.lstrip(".").split("."))
        if ip is None:                                  # not a decodable ipv4/ipv6 InetAddress index -> skip
            continue
        mac = _hexmac_to_colon(value)
        if mac in _NULL_MACS:                           # incomplete/expired neighbour slot -> noise, skip
            continue
        out.append({"ip": ip, "mac": mac, "hostname": None})
    return out


def _parse_dnsmasq_leases(text, parse):
    """dnsmasq lease-file text (one lease per line, `<expiry> <mac> <ip> <hostname> <clientid>`) -> [{ip, mac,
    hostname}] — the OpenWrt AP DHCP method. Vendor-blind + hostile-tolerant like the other parsers: a line with <4
    whitespace fields is SKIPPED; a `*` hostname (dnsmasq "no name") degrades hostname->None; the expiry + client-id
    columns are ignored. Values are RAW here (the pure READ side re-validates ip/mac/hostname before the GUI — a bad IP
    drops the row, a bad mac/hostname -> None). A non-str input -> []; a malformed line NEVER raises (a hostile AP
    blanks its own source, not the sweep). `parse` is accepted for _PARSERS signature-parity (this shape reads no
    descriptor fields — the dnsmasq column order is intrinsic to the format, like arp_table's last-4-OID rule)."""
    out = []
    for line in (text if isinstance(text, str) else "").splitlines():
        f = line.split()
        if len(f) < 4:
            continue
        mac, ip, hostname = f[1], f[2], f[3]
        out.append({"ip": ip, "mac": mac, "hostname": None if hostname == "*" else hostname})
    return out


_PARSERS = {"json_rows": _parse_json_rows,
            "arp_table": _parse_snmp_arp_table,
            "neighbors_table": _parse_snmp_neighbors_table,   # RFC-4293 dual-stack ipNetToPhysicalTable
            "lease_file": _parse_dnsmasq_leases}              # dnsmasq lease-file columns (an OpenWrt AP over ssh)


# --- the offer + the sweep --------------------------------------------------------------------------------------
def offered_methods(module, methods_by):
    """The discovery descriptors an onboarded class OFFERS — its DECLARED `discovery:` block references resolved
    against the registry. Declared-only (F2): opnsense ships no httpapi plugin, so a facts predicate would reach
    NOTHING — a class opts in explicitly, exactly like logging's proxmox_api. A method the registry doesn't carry
    is skipped (a typo can't crash the sweep). Vendor-blind: reads the descriptor SHAPE, never a vendor literal."""
    out = []
    for ref in (module.get("discovery") or []):
        if not isinstance(ref, dict):
            continue
        d = methods_by.get(ref.get("method"))
        if d:
            out.append(d)
    return out


def _load_module(key):
    """modules/<key>/module.yml parsed (via the overlay-aware paths.module_file), or None if absent/malformed (a
    typo in one class can't blank the sweep)."""
    mp = paths.module_file(key)
    if not os.path.exists(mp):
        return None
    try:
        with open(mp, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return None


def _dispatch_parse(desc, data):
    """Shared parse + flood-cap: look up the descriptor's `parse.shape` in _PARSERS (a NAME, ZERO vendor branch),
    parse `data` (an HTTP body STRING for json_rows, a list of net-snmp walk LINES for arp_table — each parser
    accepts its own transport's shape), and count-cap the rows (the flood cap's parse half). Unknown shape ->
    ('bad-shape', []). Returns (status, rows)."""
    fn = _PARSERS.get((desc.get("parse") or {}).get("shape"))
    if fn is None:
        return "bad-shape", []
    rows = fn(data, desc.get("parse") or {})
    return "ok", rows[:MAX_ROWS]


def _run_http(desc, host, fetchers, environ):
    """Read ONE http-transport method against ONE onboarded host (the pre-Rung-3 path, extracted VERBATIM so the
    json_rows DHCP methods stay byte-identical). Reach-then-judge: no-creds / unreachable / http-<code> / ok."""
    source = desc.get("source") or {}
    endpoint = (source.get("endpoint") or "").replace("${DEVICE_ADDR}", host)
    try:
        headers = _auth_headers(source.get("auth") or {}, environ)
    except KeyError:
        return "no-creds", []
    verify = bool(source.get("verify", False))
    try:
        resp = fetchers.http_get(endpoint, headers=headers, verify=verify)
    except (urllib.error.URLError, OSError):
        return "unreachable", []
    if resp.get("status") != 200:
        return "http-%s" % resp.get("status"), []
    return _dispatch_parse(desc, resp.get("body") or "")


def _run_snmp(desc, host, fetchers, environ):
    """Read ONE snmp-transport method against ONE onboarded host: a READ-ONLY SNMPv3 WALK of the descriptor's column
    OID. The creds are env NAMES the descriptor's `auth` block declares (KONTROLL_SNMP_V3_*, exported by the operator
    from the snmp_observability SOPS domain — NOT deploy-provisioned); ANY unset -> `no-creds` and the fetcher is
    NEVER called (fail-closed, no v1/v2c/authNoPriv fallback). A transport/timeout/missing-net-snmp failure ->
    `unreachable`. On a successful walk, parse the varbind lines via the shape's parser + count-cap."""
    source = desc.get("source") or {}
    auth = source.get("auth") or {}
    user = environ.get(auth.get("user_env") or "")
    auth_pass = environ.get(auth.get("auth_pass_env") or "")
    priv_pass = environ.get(auth.get("priv_pass_env") or "")
    if not (user and auth_pass and priv_pass):
        return "no-creds", []
    try:
        lines = fetchers.snmp_walk(host, source.get("walk_oid") or "", user, auth_pass, priv_pass,
                                   auth.get("auth_proto") or "SHA", auth.get("priv_proto") or "AES")
    except (OSError, subprocess.SubprocessError):
        return "unreachable", []
    return _dispatch_parse(desc, lines)


def _run_ssh(desc, host, fetchers, environ):
    """Read ONE ssh-transport method against ONE onboarded host: a READ-ONLY, forced-command SSH read of the device's
    own file (an OpenWrt AP's dnsmasq lease file). The key + pinned-known_hosts PATHS are env NAMES the descriptor's
    `auth` block declares (KONTROLL_OPENWRT_DISCOVERY_KEYFILE / _KNOWN_HOSTS — operator-exported: the operator decrypts
    the dedicated forced-command read key from the `network` SOPS domain to an ephemeral 0600 file and exports its PATH
    + the pinned known_hosts PATH before the on-demand sweep, NOT deploy-provisioned). ANY unset -> `no-creds` and the
    fetcher is NEVER called (fail-closed: no password fallback, no host-key TOFU). A transport/timeout/host-key-mismatch/
    missing-ssh-client failure -> `unreachable`. The remote command is FORCED on the device's authorized key — the
    client sends NONE (the argv ends at `<user>@<host>`), so the sweep cannot choose what runs. On a successful read,
    parse the file text via the shape's parser + count-cap. Mirrors _run_snmp's reach-then-judge shape."""
    source = desc.get("source") or {}
    auth = source.get("auth") or {}
    keyfile = environ.get(auth.get("keyfile_env") or "")
    known_hosts = environ.get(auth.get("known_hosts_env") or "")
    user = source.get("user") or "root"
    if not (keyfile and known_hosts):
        return "no-creds", []
    try:
        text = fetchers.ssh_read(host, user, keyfile, known_hosts)
    except (OSError, subprocess.SubprocessError):
        return "unreachable", []
    return _dispatch_parse(desc, text)


_TRANSPORT_RUN = {"http": _run_http, "snmp": _run_snmp, "ssh": _run_ssh}


def _run_method(desc, host, fetchers, environ):
    """Read ONE offered method against ONE onboarded host -> (status, rows), dispatching on `source.transport` (a
    NAME, ZERO vendor branch; DEFAULT 'http' so the DHCP json_rows descriptors — which carry no `transport` key — run
    the byte-identical pre-Rung-3 path). An unknown transport is fail-soft: ('bad-transport', []). All the per-status
    tolerance (no-creds / unreachable / http-<code> / ok) lives in the per-transport runner."""
    transport = (desc.get("source") or {}).get("transport") or "http"
    run = _TRANSPORT_RUN.get(transport)
    if run is None:
        return "bad-transport", []
    return run(desc, host, fetchers, environ)


def _now_iso():
    """The UTC ISO-8601 provenance stamp the artifact carries (control-VM wall clock; NOT read by any consumer for
    logic — provenance only)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sweep(fleet_doc, groups, methods_by, fetchers, environ, only_key=None, audit=None):
    """The pure dispatch core: for each ENABLED class that OFFERS a discovery method, read the method against every
    onboarded host in the class's inventory group, and assemble the artifact dict. `groups` is
    `fleet._inventory_groups()` ({group: {host: hostvars}}); `methods_by` is {name: descriptor}. Injectable
    everything (fleet_doc/groups/methods_by/fetchers/environ) so the whole sweep is hermetically testable. `audit`
    is an optional `(key, method, status, count) -> None` sink for the per-source run_id-correlated line."""
    sources, leases = [], []
    for key in paths.enabled_keys(fleet_doc):               # a fleet line that is not a class key is skipped —
        if only_key and key != only_key:                     # handed on, it raised out of the whole sweep
            continue
        module = _load_module(key)
        if not module:
            continue
        group = module.get("inventory_group")
        hosts = sorted(v.get("ansible_host") for v in (groups.get(group) or {}).values()
                       if isinstance(v, dict) and v.get("ansible_host"))
        for desc in offered_methods(module, methods_by):
            for host in hosts:
                status, rows = _run_method(desc, host, fetchers, environ)
                for r in rows:
                    r["source_key"], r["source_host"] = key, host
                    leases.append(r)
                sources.append({"key": key, "method": desc.get("name"), "host": host,
                                "status": status, "count": len(rows)})
                if audit:
                    audit(key, desc.get("name"), status, len(rows))
    return {"schema": SCHEMA, "generated_at": _now_iso(), "sources": sources, "leases": leases}


def write_artifact(result, path=None):
    """Write the sweep result to the git-ignored inbox artifact (the ONE write — a top-level actor write, NOT a
    service-layer verb). Creates `local/` if absent; the pure `service/discovery.read_inbox` reads it back."""
    path = path or discovery_svc.inbox_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
    return path


def run(only_key=None, fetchers=None, audit_path=None):
    """Load the enabled fleet + onboarded inventory + the discovery registry, sweep, audit per source (NAMES only),
    and write the artifact. Returns the result dict. The one non-hermetic entry (it reaches the lab via the real
    _Fetchers unless one is injected)."""
    fetchers = fetchers or _Fetchers()
    audit_path = audit_path or _audit_path()
    fleet_doc = yaml.safe_load(open(paths.resolve("config/fleet.yml"), encoding="utf-8")) or {}
    groups = fleet_svc._inventory_groups()
    methods_by = {m["name"]: m for m in catalog.load_discovery()}
    run_id = mint_run_id()

    def _audit(key, method, status, count):
        write_audit(audit_path, "discover-cli", "local", "discover", run_id,
                    "%s %s %s count=%d" % (key, method, status, count))   # NAMES/counts only — never a cred/lease (C12)

    result = sweep(fleet_doc, groups, methods_by, fetchers, environ=os.environ, only_key=only_key, audit=_audit)
    write_artifact(result)
    return result


def _print(result):
    srcs = result.get("sources") or []
    if not srcs:
        print("kontroll-discover: no enabled class offers a discovery method (nothing to sweep)")
        return
    for s in srcs:
        print("%-11s %-24s %s (%d leases)" % (s["status"], s["key"], s["method"], s["count"]))
    n_ok = sum(s["status"] == "ok" for s in srcs)
    print("-- %d sources: %d ok, %d leases -> %s"
          % (len(srcs), n_ok, len(result.get("leases") or []), discovery_svc.inbox_path()))


def main(argv):
    ap = argparse.ArgumentParser(description="Passive discovery sweep — read onboarded devices' own lease views (read-only).")
    ap.add_argument("--class", dest="cls", help="restrict the sweep to one enabled module key")
    args = ap.parse_args(argv)
    _print(run(only_key=args.cls))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
