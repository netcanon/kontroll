"""discovery-inbox Rung 1a — the passive-discovery read spine (registry + sweep actor + pure read_inbox).

WHY these tests (the failures they guard): the whole feature's safety claim is "zero new authority + passive +
read-only", so each test pins one leg of that — the DECLARED offer actually reaches opnsense (a facts predicate
would reach nothing, F2), the fetcher exposes ONLY a GET (no scan/write primitive can creep in, C18), a hostile
device response is DATA not code (dropped/normalized, never an injection into the result), the IP-diff hides an
already-onboarded host, the flood cap bounds a malicious lease flood, and — the load-bearing one — `read_inbox`
stays `assert_read_only`-green so the read view can never gain a write/actuate path (the design-of-record's
out-of-band-cached-model proof). Design-of-record: docs/reviews/2026-07-03-discovery-inbox-scope/99-synthesis.md.
"""
import importlib.util
import json
import os

import pytest
import yaml

import _readonly_pins
from kontroll import catalog, predicate
from kontroll.service import discovery

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "discovery", "opnsense_searchlease.json")
_CREDS = {"KONTROLL_OPNSENSE_DISCOVERY_KEY": "k", "KONTROLL_OPNSENSE_DISCOVERY_SECRET": "s"}


def _load_actor():
    """Load the hyphenated top-level sweep actor as a module (the same importlib idiom test_autopromoter uses)."""
    spec = importlib.util.spec_from_file_location("kontroll_discover",
                                                  os.path.join(ROOT, "scripts", "kontroll-discover.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


discover = _load_actor()


class _FakeFetchers:
    """A hermetic stand-in for the real device-reaching _Fetchers: returns a scripted body/status for any GET (or
    raises a scripted transport error), recording each call. Same 1-method surface, so the sweep's dispatch/parse
    runs with no network."""
    def __init__(self, body="", status=200, raise_exc=None):
        self.body, self.status, self.raise_exc = body, status, raise_exc
        self.calls = []

    def http_get(self, url, headers=None, verify=False, ca_file=None):
        self.calls.append({"url": url, "headers": headers or {}, "verify": verify})
        if self.raise_exc:
            raise self.raise_exc
        return {"status": self.status, "body": self.body}


def _fixture_body():
    with open(FIXTURE, encoding="utf-8") as fh:
        return fh.read()


def _fixture(name):
    """Read a named discovery fixture body (a vendor lease-API response golden), for the Rung-2 sibling methods."""
    with open(os.path.join(ROOT, "tests", "fixtures", "discovery", name), encoding="utf-8") as fh:
        return fh.read()


def _arp_lines():
    """The net-snmp ipNetToMediaTable walk golden as a list of lines (the shape _run_snmp/_parse_snmp_arp_table see)."""
    return _fixture("cisco_ios_arp_walk.txt").splitlines()


def _neighbor_lines():
    """The net-snmp ipNetToPhysicalTable (RFC-4293 dual-stack) walk golden — the shape _parse_snmp_neighbors_table sees."""
    return _fixture("cisco_ios_neighbor_walk.txt").splitlines()


_SNMP_CREDS = {"KONTROLL_SNMP_V3_USER": "u", "KONTROLL_SNMP_V3_AUTH_PASS": "a", "KONTROLL_SNMP_V3_PRIV_PASS": "p"}


class _FakeSnmpFetchers:
    """Hermetic stand-in for the SNMP transport (Rung 3): returns scripted walk LINES for any snmp_walk (or raises a
    scripted transport error), recording each call. Same 1-method surface as the real _Fetchers.snmp_walk, so the
    sweep's snmp dispatch/parse runs with no net-snmp and no device."""
    def __init__(self, lines=None, raise_exc=None):
        self.lines, self.raise_exc = lines or [], raise_exc
        self.calls = []

    def snmp_walk(self, host, oid, user, auth_pass, priv_pass, auth_proto="SHA", priv_proto="AES"):
        self.calls.append({"host": host, "oid": oid, "user": user, "auth_proto": auth_proto, "priv_proto": priv_proto})
        if self.raise_exc:
            raise self.raise_exc
        # `lines` may be a flat list (returned for any OID) OR a {oid: lines} map (so a class offering BOTH the v4
        # ipNetToMedia + the dual-stack ipNetToPhysical methods gets the RIGHT golden per walked column).
        return list(self.lines.get(oid, []) if isinstance(self.lines, dict) else self.lines)


# --- registry + offer -----------------------------------------------------------------------------------------
def test_registry_loads_and_sorts_by_order():
    """load_discovery() reads the drop-in descriptor(s), each with the required shape (name/source/parse), sorted by
    `order` — the 4th sorted-glob registry. Guards a malformed descriptor blanking the registry or a loader that
    forgets to sort (adding a method must be a pure drop-in a test reads, never a loader edit)."""
    methods = catalog.load_discovery()
    assert methods, "no discovery methods loaded"
    orders = [m.get("order", 0) for m in methods]
    assert orders == sorted(orders), "load_discovery must return descriptors sorted by `order`"
    assert catalog.registered_discovery_methods() == [m["name"] for m in methods], \
        "registered_discovery_methods must mirror load_discovery's order (the neutral registry-keys list)"
    for m in methods:
        assert m.get("name") and m.get("source") and m.get("parse", {}).get("shape")
    names = catalog.registered_discovery_methods()
    for method in ("dhcp_leases_fortigate", "dhcp_leases_opnsense", "dhcp_leases_openwrt", "dhcp_leases_routeros",
                   "arp_neighbors_snmp", "neighbors_snmp"):
        assert method in names, "missing discovery method %s" % method


def test_offer_reaches_opnsense_via_declared_block_not_facts():
    """The opnsense class OFFERS dhcp_leases_opnsense through its DECLARED `discovery:` block — AND a
    `{plugin: httpapi}` facts predicate would offer to NOTHING against its REAL facts.pinned.yml (F2/MF-1). This is
    the regression guard against ever "simplifying" the offer to a facts predicate: opnsense ships no connection
    plugin, so that would silently make discovery unavailable on the one class that has it."""
    module = yaml.safe_load(open(os.path.join(ROOT, "modules", "opnsense", "module.yml"), encoding="utf-8"))
    facts = yaml.safe_load(open(os.path.join(ROOT, "modules", "opnsense", "facts.pinned.yml"), encoding="utf-8"))
    methods_by = {m["name"]: m for m in catalog.load_discovery()}
    offered = [m["name"] for m in discover.offered_methods(module, methods_by)]
    assert "dhcp_leases_opnsense" in offered, "the declared block must reach opnsense"
    assert predicate.eval_pred({"plugin": "httpapi"}, facts) is not True, \
        "a {plugin: httpapi} predicate must NOT match opnsense (why the offer is declared, not facts-derived)"


def test_offer_is_declared_only():
    """A class with no `discovery:` block offers nothing (the offer is opt-in per class, never universal) — so
    onboarding a class doesn't silently start reaching it for leases."""
    methods_by = {m["name"]: m for m in catalog.load_discovery()}
    assert discover.offered_methods({}, methods_by) == []
    assert discover.offered_methods({"discovery": [{"method": "no-such-method"}]}, methods_by) == []


def test_offer_reaches_fortigate_and_routeros_via_declared_blocks():
    """Rung 2 — the fortigate + routeros classes each OFFER their DHCP-lease sibling method through the DECLARED
    `discovery:` block against their REAL module.yml. Guards the codeless-drop-in wiring (a new descriptor file + a
    one-line offer block must actually reach the class). Declared-not-derived is deliberate even for fortigate, which
    SHIPS an httpapi plugin — a `{plugin: httpapi}` predicate WOULD match it, yet the offer stays opt-in so onboarding
    a firewall never silently starts reading its leases (routeros ships cliconf — it is READ over REST regardless)."""
    methods_by = {m["name"]: m for m in catalog.load_discovery()}
    for key, method in [("fortigate", "dhcp_leases_fortigate"), ("routeros", "dhcp_leases_routeros")]:
        module = yaml.safe_load(open(os.path.join(ROOT, "modules", key, "module.yml"), encoding="utf-8"))
        offered = [m["name"] for m in discover.offered_methods(module, methods_by)]
        assert method in offered, "the declared block must reach %s" % key
    ffacts = yaml.safe_load(open(os.path.join(ROOT, "modules", "fortigate", "facts.pinned.yml"), encoding="utf-8"))
    assert predicate.eval_pred({"plugin": "httpapi"}, ffacts) is True, \
        "fortigate DOES ship httpapi — the declared-not-derived offer is a deliberate opt-in, not an opnsense-style workaround"


def test_offer_reaches_cisco_ios_arp_via_declared_block():
    """Rung 3: the cisco_ios class OFFERS arp_neighbors_snmp through its DECLARED `discovery:` block against its REAL
    module.yml. Guards the SNMP method's offer wiring — a class opts in explicitly, so onboarding a switch never
    silently starts walking its ARP table."""
    module = yaml.safe_load(open(os.path.join(ROOT, "modules", "cisco_ios", "module.yml"), encoding="utf-8"))
    methods_by = {m["name"]: m for m in catalog.load_discovery()}
    assert "arp_neighbors_snmp" in [m["name"] for m in discover.offered_methods(module, methods_by)]


# --- the parser (vendor-blind, hostile-tolerant) --------------------------------------------------------------
def test_parse_json_rows_maps_declared_fields():
    """_parse_json_rows maps the descriptor's `parse.fields` (address->ip, hwaddr->mac, hostname->hostname) over the
    `rows` array — the de-bespoke seam (no function knows "opnsense"; the columns are DATA). Guards a field-map
    regression that would surface the wrong column as the IP."""
    parse = catalog.discovery_method("dhcp_leases_opnsense")["parse"]
    rows = discover._parse_json_rows(_fixture_body(), parse)
    assert len(rows) == 3
    assert {"ip": "192.0.2.50", "mac": "00:00:5e:00:53:01", "hostname": "printer"} in rows


def test_parse_json_rows_tolerates_hostile_body():
    """A non-JSON body, a JSON non-object, and an object whose rows field is missing/non-list all yield [] — never a
    raise. Guards a hostile/misconfigured device response crashing the sweep (a source blanks itself, not the run)."""
    parse = {"shape": "json_rows", "rows": "rows", "fields": {"ip": "address"}}
    assert discover._parse_json_rows("not json {", parse) == []
    assert discover._parse_json_rows("[1, 2, 3]", parse) == []          # bare list of non-dicts
    assert discover._parse_json_rows('{"rows": "nope"}', parse) == []   # rows not a list
    assert discover._parse_json_rows('{"total": 0}', parse) == []       # rows field absent


def test_parse_json_rows_results_key_fortigate():
    """Rung 2 (codeless): the fortigate method reads a `results`-keyed body (the FortiOS monitor shape) with
    ip/mac/hostname columns — a pure DESCRIPTOR difference (`rows: results`, its own field map), NO parser code
    change. Guards the Rung-2 claim that a vendor sibling is a drop-in the SAME `_parse_json_rows` already serves."""
    parse = catalog.discovery_method("dhcp_leases_fortigate")["parse"]
    rows = discover._parse_json_rows(_fixture("fortigate_dhcp.json"), parse)
    assert len(rows) == 3
    assert {"ip": "192.0.2.50", "mac": "00:00:5e:00:53:01", "hostname": "printer"} in rows


def test_parse_json_rows_bare_array_routeros():
    """Rung 2 (codeless): the routeros method reads a BARE top-level JSON array (RouterOS REST returns `[ {...}, ...]`
    with NO wrapper key) whose columns are hyphenated (address/mac-address/host-name) — again a pure descriptor
    difference (no `rows:`, its own field map). Guards the parser's bare-array fall-through + the drop-in claim for a
    vendor whose response shape has no envelope."""
    parse = catalog.discovery_method("dhcp_leases_routeros")["parse"]
    assert "rows" not in parse, "routeros is the bare-array path — a `rows:` key would change the shape"
    rows = discover._parse_json_rows(_fixture("routeros_dhcp.json"), parse)
    assert len(rows) == 3
    assert {"ip": "192.0.2.10", "mac": "00:00:5e:00:53:03", "hostname": "core-switch"} in rows


def test_parse_arp_table_reconstructs_ip_from_suffix_and_mac_from_value():
    """Rung 3: _parse_snmp_arp_table reads the net-snmp ipNetToMediaTable golden -> the IPv4 is the last 4 OID
    sub-identifiers (the row-index suffix) and the MAC is the Hex-STRING value (normalized to colon-lower); an
    ARP neighbour has no hostname (None). The incomplete-ARP row (all-zero MAC) is DROPPED as noise. Guards the
    single-column suffix-IP + value-MAC reconstruction AND the noise filter (a clean first dogfood vs phantom rows)."""
    assert discover._parse_snmp_arp_table(_arp_lines(), {}) == [
        {"ip": "192.0.2.50", "mac": "00:00:5e:00:53:01", "hostname": None},
        {"ip": "192.0.2.51", "mac": "00:00:5e:00:53:02", "hostname": None},
        {"ip": "192.0.2.10", "mac": "00:00:5e:00:53:03", "hostname": None}]


def test_parse_arp_table_tolerates_hostile_output():
    """A device byte is DATA: a line with no ` = `, a `No Such Instance` value (a per-instance MIB error, NOT a real
    neighbour), and an OID with <4 sub-ids all yield NO row (never a raise, never a phantom candidate); a valid-OID
    line with a non-MAC value keeps the IP with mac->None. A non-list input -> []. Guards a hostile/mis-answering
    switch fabricating a candidate or crashing the sweep."""
    lines = ["garbage with no equals",
             ".1.3.6.1.2.1.4.22.1.2.3.192.0.2.9 = No Such Instance currently exists at this OID",
             ".1.2.3 = Hex-STRING: 00 00 5E 00 53 07",                       # OID too short (<4 sub-ids)
             ".1.3.6.1.2.1.4.22.1.2.3.192.0.2.8 = Wrong Type (should be OCTET STRING)"]
    assert discover._parse_snmp_arp_table(lines, {}) == [{"ip": "192.0.2.8", "mac": None, "hostname": None}]
    assert discover._parse_snmp_arp_table("not a list", {}) == []


def test_hexmac_to_colon_normalizes_and_rejects():
    """_hexmac_to_colon accepts the net-snmp Hex-STRING (space), colon, and dash forms -> a lowercase colon MAC, and
    rejects anything that is not EXACTLY 6 hex octets -> None (a junk value degrades the mac, never the row's IP).
    Guards a MAC-normalization regression surfacing an un-normalized device byte. ALSO the collapsed-leading-zero
    `STRING:` form (net-snmp renders ipNetToMediaPhysAddress WITHOUT the IP-MIB DISPLAY-HINT on the runtime image,
    so a sub-0x10 octet prints as a single digit) — live-caught on the cisco SNMP-ARP dogfood, where a real switch
    neighbour with a `00`-octet MAC was silently dropped; each octet is now zero-padded to 2 digits."""
    assert discover._hexmac_to_colon("Hex-STRING: 00 00 5E 00 53 01") == "00:00:5e:00:53:01"
    assert discover._hexmac_to_colon("00:00:5E:00:53:02") == "00:00:5e:00:53:02"
    assert discover._hexmac_to_colon("00-00-5e-00-53-03") == "00:00:5e:00:53:03"
    assert discover._hexmac_to_colon("STRING: 0:0:5e:0:53:1") == "00:00:5e:00:53:01"   # collapsed form (RFC-7042 doc MAC)
    assert discover._hexmac_to_colon("0:0:5e:0:53:a") == "00:00:5e:00:53:0a"           # multi + low-octet collapse
    assert discover._hexmac_to_colon("Hex-STRING: 00 00 5E 00 53 01 02") is None   # 7 octets -> reject
    assert discover._hexmac_to_colon("xyz:0:5e:0:53:1") is None                    # non-hex octet -> reject
    assert discover._hexmac_to_colon("not a mac") is None


# --- the fetcher is passive-only by construction --------------------------------------------------------------
def test_fetchers_expose_only_read_only_primitives():
    """The _Fetchers surface is EXACTLY three read-only device primitives (Rung 3 + the OpenWrt rung): http_get
    (hard-wired GET), snmp_walk (net-snmp `snmpbulkwalk` — GETBULK), and ssh_read (a forced-command `ssh` read).
    All three are structurally read-only — no write/POST/SET/transfer verb is DEFINED, so none can be reached — the
    in-code twin of the discovery-passive-only grep-gate. The surface (not just each verb) is asserted so a future
    write/scan/transfer primitive creeping into the sweep fails loudly here."""
    import inspect
    public = [m for m in dir(discover._Fetchers)
              if not m.startswith("_") and callable(getattr(discover._Fetchers, m))]
    assert public == ["http_get", "snmp_walk", "ssh_read"], \
        "the fetcher must expose ONLY read-only primitives, got %r" % public
    http_src = inspect.getsource(discover._Fetchers.http_get)
    assert 'method="GET"' in http_src and "POST" not in http_src and "data=" not in http_src
    snmp_src = inspect.getsource(discover._Fetchers.snmp_walk)
    assert "snmpbulkwalk" in snmp_src and "snmpset" not in snmp_src and "snmpbulkset" not in snmp_src
    ssh_src = inspect.getsource(discover._Fetchers.ssh_read)     # the OpenWrt rung's forced-command read primitive
    for tok in ("scp", "sftp", "ProxyCommand", "RemoteCommand", "LocalCommand", "LocalForward", "RemoteForward",
                "DynamicForward", "ForwardAgent", "ForwardX11", "ProxyJump", "Subsystem", "Tunnel"):
        assert tok not in ssh_src, "ssh_read must not contain %r (a write/tunnel/remote-command SSH construct)" % tok


# --- the sweep (hermetic via the fake fetcher) ----------------------------------------------------------------
def _sweep_opnsense(fetchers, environ=_CREDS, monrows=None):
    """Run the sweep with opnsense enabled + one edge_firewall host, the REAL registry, an injected fetcher."""
    fleet_doc = {"enabled_modules": ["opnsense"]}
    groups = {"edge_firewall": {"fw1": {"ansible_host": "192.0.2.1"}}}
    methods_by = {m["name"]: m for m in catalog.load_discovery()}
    return discover.sweep(fleet_doc, groups, methods_by, fetchers, environ)


def test_sweep_reads_offered_class_and_tags_each_lease():
    """End-to-end (hermetic): enabling opnsense + one host makes the sweep resolve the DECLARED method against the
    REAL module.yml, GET the (faked) lease API, parse it, and tag every lease with its source class+host. Guards the
    whole offer->reach->parse->assemble wiring the read view depends on."""
    fk = _FakeFetchers(body=_fixture_body())
    result = _sweep_opnsense(fk)
    assert result["schema"] == 1 and isinstance(result["generated_at"], str)
    assert len(fk.calls) == 1 and fk.calls[0]["url"].endswith("/api/dhcpv4/leases/searchLease")
    assert fk.calls[0]["headers"].get("Authorization", "").startswith("Basic ")   # creds composed at the boundary
    assert result["sources"] == [{"key": "opnsense", "method": "dhcp_leases_opnsense",
                                  "host": "192.0.2.1", "status": "ok", "count": 3}]
    assert {l["ip"] for l in result["leases"]} == {"192.0.2.50", "192.0.2.51", "192.0.2.10"}
    assert all(l["source_key"] == "opnsense" and l["source_host"] == "192.0.2.1" for l in result["leases"])


def test_sweep_no_creds_when_env_unset_and_never_reaches():
    """With the cred env UNSET the source is recorded `no-creds` with zero leases AND the fetcher is never called —
    the sweep never reaches a device it has no token for (no privileged side-channel, no crash)."""
    fk = _FakeFetchers(body=_fixture_body())
    result = _sweep_opnsense(fk, environ={})
    assert result["sources"][0]["status"] == "no-creds" and result["sources"][0]["count"] == 0
    assert result["leases"] == [] and fk.calls == []


def test_sweep_unreachable_is_tolerated():
    """A transport failure records the source `unreachable` (zero leases) and the sweep completes — a down device is
    tolerated, never fatal (the ignore-unreachable ethos)."""
    import urllib.error
    fk = _FakeFetchers(raise_exc=urllib.error.URLError("down"))
    result = _sweep_opnsense(fk)
    assert result["sources"][0]["status"] == "unreachable" and result["leases"] == []


def test_sweep_non_200_is_recorded():
    """A non-200 (e.g. a 403 from a too-narrow key) is recorded as `http-403` with zero leases — an auth/endpoint
    problem surfaces as a status, never a parsed-garbage lease."""
    fk = _FakeFetchers(body="forbidden", status=403)
    result = _sweep_opnsense(fk)
    assert result["sources"][0]["status"] == "http-403" and result["leases"] == []


def test_sweep_fortigate_bearer_and_results_end_to_end():
    """Rung 2 end-to-end (hermetic): enabling fortigate + one edge host makes the sweep resolve the DECLARED fortigate
    method against the REAL module.yml, GET the (faked) monitor API with a `Bearer` token (the `custom` auth strategy,
    NOT Basic), parse the `results` array, and tag each lease. Guards the whole offer->reach->parse path for a
    custom-auth vendor + that `custom` auth composes the Authorization header verbatim (no base64)."""
    fleet_doc = {"enabled_modules": ["fortigate"]}
    groups = {"edge_firewall": {"fw1": {"ansible_host": "192.0.2.1"}}}
    methods_by = {m["name"]: m for m in catalog.load_discovery()}
    fk = _FakeFetchers(body=_fixture("fortigate_dhcp.json"))
    result = discover.sweep(fleet_doc, groups, methods_by, fk, {"KONTROLL_FORTIGATE_DISCOVERY_TOKEN": "tok"})
    assert len(fk.calls) == 1 and fk.calls[0]["url"].endswith("/api/v2/monitor/system/dhcp")
    assert fk.calls[0]["headers"].get("Authorization") == "Bearer tok"   # custom auth -> verbatim header, no base64
    assert result["sources"][0] == {"key": "fortigate", "method": "dhcp_leases_fortigate",
                                    "host": "192.0.2.1", "status": "ok", "count": 3}
    assert {l["ip"] for l in result["leases"]} == {"192.0.2.50", "192.0.2.51", "192.0.2.10"}


def test_sweep_routeros_bare_array_end_to_end():
    """Rung 2 end-to-end (hermetic): the routeros class (STAGED, but its module.yml declares the offer) sweeps its
    BARE-array REST lease body through Basic auth. Proves a staged class's declared offer resolves once enabled AND
    the bare-array shape flows offer->reach->parse->assemble exactly like an enveloped one."""
    fleet_doc = {"enabled_modules": ["routeros"]}
    groups = {"core_switch": {"sw1": {"ansible_host": "192.0.2.2"}}}
    methods_by = {m["name"]: m for m in catalog.load_discovery()}
    fk = _FakeFetchers(body=_fixture("routeros_dhcp.json"))
    result = discover.sweep(fleet_doc, groups, methods_by, fk,
                            {"KONTROLL_ROUTEROS_DISCOVERY_USER": "u", "KONTROLL_ROUTEROS_DISCOVERY_PASSWORD": "p"})
    assert len(fk.calls) == 1 and fk.calls[0]["url"].endswith("/rest/ip/dhcp-server/lease")
    assert fk.calls[0]["headers"].get("Authorization", "").startswith("Basic ")   # basic auth composed at boundary
    assert result["sources"][0] == {"key": "routeros", "method": "dhcp_leases_routeros",
                                    "host": "192.0.2.2", "status": "ok", "count": 3}
    assert {l["ip"] for l in result["leases"]} == {"192.0.2.50", "192.0.2.51", "192.0.2.10"}


# --- Rung 3: the SNMP transport (dispatch + walk + parse, hermetic via _FakeSnmpFetchers) ----------------------
def _sweep_cisco(fetchers, environ=None):
    """Run the sweep with cisco_ios enabled + one core_switch host, the REAL registry, an injected SNMP fetcher."""
    return discover.sweep({"enabled_modules": ["cisco_ios"]},
                          {"core_switch": {"sw1": {"ansible_host": "192.0.2.2"}}},
                          {m["name"]: m for m in catalog.load_discovery()}, fetchers,
                          _SNMP_CREDS if environ is None else environ)


def test_run_method_default_transport_is_http_snmp_is_dispatched():
    """The transport seam: a descriptor with NO `source.transport` runs the http path (so the 3 DHCP json_rows
    methods are byte-identical), the arp method's `transport: snmp` dispatches to _run_snmp, and an unknown transport
    is fail-soft `bad-transport`. Guards the second transport from silently changing the DHCP methods' behaviour."""
    assert discover._TRANSPORT_RUN == {"http": discover._run_http, "snmp": discover._run_snmp,
                                       "ssh": discover._run_ssh}
    fk = _FakeFetchers(body=_fixture_body())
    status, _ = discover._run_method(catalog.discovery_method("dhcp_leases_opnsense"), "192.0.2.1", fk, _CREDS)
    assert status == "ok" and len(fk.calls) == 1                         # json_rows still uses http_get
    assert discover._run_method({"source": {"transport": "carrier-pigeon"}}, "192.0.2.1", fk, {}) == ("bad-transport", [])


def test_sweep_cisco_ios_arp_snmp_end_to_end():
    """Rung 3 end-to-end (hermetic): enabling cisco_ios + one core_switch host makes the sweep resolve the DECLARED
    arp_neighbors_snmp method against the REAL module.yml, dispatch to the SNMP transport (snmp_walk of the
    ipNetToMediaPhysAddress column with the authPriv creds resolved from env), parse the (faked) walk lines, and tag
    each neighbour (hostname None — ARP has none). Guards the whole offer->snmp-transport->parse->assemble path."""
    # cisco_ios OFFERS both arp_neighbors_snmp (v4) + neighbors_snmp (dual-stack) -> two walks; the OID-aware fake
    # returns the ipNetToMedia golden ONLY for the v4 column (the neighbors column gets [] here — its own test below).
    fk = _FakeSnmpFetchers(lines={"1.3.6.1.2.1.4.22.1.2": _arp_lines()})
    result = _sweep_cisco(fk)
    assert len(fk.calls) == 2 and {c["oid"] for c in fk.calls} == {"1.3.6.1.2.1.4.22.1.2", "1.3.6.1.2.1.4.35.1.4"}
    arp = next(s for s in result["sources"] if s["method"] == "arp_neighbors_snmp")
    assert arp == {"key": "cisco_ios", "method": "arp_neighbors_snmp", "host": "192.0.2.2", "status": "ok", "count": 3}
    assert {l["ip"] for l in result["leases"]} == {"192.0.2.50", "192.0.2.51", "192.0.2.10"}
    assert all(l["hostname"] is None and l["source_key"] == "cisco_ios" for l in result["leases"])


def test_sweep_snmp_no_creds_when_env_unset_and_never_walks():
    """With the SNMPv3 env UNSET the source is `no-creds` with zero rows AND snmp_walk is NEVER called — fail-closed,
    no privileged/unauth fallback (the switch is never reached without authPriv creds)."""
    fk = _FakeSnmpFetchers(lines=_arp_lines())
    result = _sweep_cisco(fk, environ={})
    assert result["sources"][0]["status"] == "no-creds" and result["sources"][0]["count"] == 0
    assert result["leases"] == [] and fk.calls == []


def test_sweep_snmp_unreachable_is_tolerated():
    """A transport failure (device down / net-snmp missing / auth fail) records `unreachable` with zero rows and the
    sweep completes — a down switch is tolerated, never fatal."""
    result = _sweep_cisco(_FakeSnmpFetchers(raise_exc=OSError("snmpbulkwalk rc=1")))
    assert result["sources"][0]["status"] == "unreachable" and result["leases"] == []


# --- IPv6 rung: the dual-stack ipNetToPhysicalTable method (neighbors_snmp) --------------------------------------
def test_offer_reaches_cisco_ios_neighbors_via_declared_block():
    """The IPv6 rung: cisco_ios OFFERS neighbors_snmp (dual-stack) through its DECLARED `discovery:` block against its
    REAL module.yml — ALONGSIDE the existing v4 arp_neighbors_snmp (a class may offer both; the read side de-dupes by
    IP). Guards the additive offer (the v4 method stays offered, so the live v4 dogfood is untouched)."""
    module = yaml.safe_load(open(os.path.join(ROOT, "modules", "cisco_ios", "module.yml"), encoding="utf-8"))
    methods_by = {m["name"]: m for m in catalog.load_discovery()}
    offered = [m["name"] for m in discover.offered_methods(module, methods_by)]
    assert "neighbors_snmp" in offered and "arp_neighbors_snmp" in offered


def test_index_to_ip_decodes_v4_and_v6_and_rejects_junk():
    """`_index_to_ip` decodes an ipNetToPhysical row-index tail `<ifIndex>.<addrType>.<len>.<addr-bytes>` -> a v4 OR v6
    string, scanning from the RIGHT (a leading ifIndex of any width is irrelevant): addrType 1/len 4 -> dotted-quad,
    2/len 16 -> canonical compressed colon-hex. A too-short tail, a type/len mismatch, an out-of-octet byte, or an
    unhandled type (ipv4z/dns) -> None. WHY: the type-tagged/length-prefixed decode is the WHOLE reason this is a new
    shape (not the v4 last-4 rule); a regression names the exact leg."""
    assert discover._index_to_ip("3.1.4.192.0.2.50".split(".")) == "192.0.2.50"          # v4: <ifIndex>.1.4.<quad>
    assert discover._index_to_ip("7.2.16.32.1.13.184.0.0.0.0.0.0.0.0.0.0.0.1".split(".")) == "2001:db8::1"  # v6
    assert discover._index_to_ip("1.1.4".split(".")) is None                              # too few sub-ids
    assert discover._index_to_ip("3.2.4.192.0.2.50".split(".")) is None                   # type=2(v6) but len 4 -> mismatch
    assert discover._index_to_ip("3.1.4.192.0.2.999".split(".")) is None                  # a byte out of octet range
    assert discover._index_to_ip("7.16.4.1.2.3.4".split(".")) is None                     # addrType 16 (dns) -> not decoded


def test_parse_neighbors_table_decodes_v4_and_v6_and_drops_noise():
    """`_parse_snmp_neighbors_table` reads the RFC-4293 ipNetToPhysicalTable golden -> exactly the three real neighbours
    (one v4, two v6 in canonical colon-hex), with the incomplete all-zero-MAC v6 row DROPPED as noise. WHY: pins the
    dual-stack decode + the same null-MAC filter as arp_table — the read-side widening depends on real v6 strings."""
    assert discover._parse_snmp_neighbors_table(_neighbor_lines(), {}) == [
        {"ip": "192.0.2.50", "mac": "00:00:5e:00:53:01", "hostname": None},
        {"ip": "2001:db8::1", "mac": "00:00:5e:00:53:02", "hostname": None},
        {"ip": "2001:db8::2", "mac": "00:00:5e:00:53:03", "hostname": None}]


def test_parse_neighbors_table_tolerates_hostile_output():
    """A device byte is DATA: a line with no ` = `, a `No Such Instance` value, an unhandled addrType index, a wrong
    length, and a valid v6 index with a non-MAC value -> the last keeps the IP with mac None, all others yield NO row;
    a non-list input -> []. WHY: a hostile/mis-answering switch can't fabricate a candidate or crash the sweep."""
    lines = ["garbage with no equals",
             ".1.3.6.1.2.1.4.35.1.4.3.1.4.192.0.2.9 = No Such Instance currently exists at this OID",
             ".1.3.6.1.2.1.4.35.1.4.7.16.4.1.2.3.4 = Hex-STRING: 00 00 5E 00 53 07",      # addrType 16 (dns) -> skip
             ".1.3.6.1.2.1.4.35.1.4.3.1.4.192.0.2.8 = Wrong Type (should be OCTET STRING)"]
    assert discover._parse_snmp_neighbors_table(lines, {}) == [{"ip": "192.0.2.8", "mac": None, "hostname": None}]
    assert discover._parse_snmp_neighbors_table("not a list", {}) == []


def _sweep_cisco_neighbors(fetchers, environ=None):
    """Run the sweep with cisco_ios enabled + one core_switch host; the OID-aware fake returns the ipNetToPhysical
    golden ONLY for the dual-stack column, so the v4 arp method sees [] and this isolates the neighbors_snmp path."""
    return discover.sweep({"enabled_modules": ["cisco_ios"]},
                          {"core_switch": {"sw1": {"ansible_host": "192.0.2.2"}}},
                          {m["name"]: m for m in catalog.load_discovery()}, fetchers,
                          _SNMP_CREDS if environ is None else environ)


def test_sweep_cisco_ios_neighbors_snmp_v6_end_to_end():
    """IPv6 rung end-to-end (hermetic): enabling cisco_ios makes the sweep resolve the DECLARED neighbors_snmp method,
    dispatch to the SNMP transport (snmp_walk of the ipNetToPhysicalPhysAddress column 1.3.6.1.2.1.4.35.1.4), parse the
    dual-stack golden, and surface BOTH the v4 and the two v6 neighbours. WHY: the whole offer->snmp->neighbors_table->
    assemble path for a dual-stack table, the corpus-growth headline (v6 ND neighbours the v4 ARP table never sees)."""
    fk = _FakeSnmpFetchers(lines={"1.3.6.1.2.1.4.35.1.4": _neighbor_lines()})
    result = _sweep_cisco_neighbors(fk)
    nbr = next(s for s in result["sources"] if s["method"] == "neighbors_snmp")
    assert nbr == {"key": "cisco_ios", "method": "neighbors_snmp", "host": "192.0.2.2", "status": "ok", "count": 3}
    assert {l["ip"] for l in result["leases"]} == {"192.0.2.50", "2001:db8::1", "2001:db8::2"}
    assert all(l["hostname"] is None and l["source_key"] == "cisco_ios" for l in result["leases"])


def test_clean_ip_is_dual_stack():
    """`service/discovery._clean_ip` now accepts a valid IPv4 OR IPv6 (the candidate key is dual-stack so a v6 neighbour
    surfaces), while an injection/junk value still degrades to None (the row is then dropped). WHY: the read-side key
    widening is what lets a v6 candidate flow — but it must keep dropping a metacharacter payload."""
    assert discovery._clean_ip("192.0.2.50") == "192.0.2.50"
    assert discovery._clean_ip("2001:db8::1") == "2001:db8::1"
    assert discovery._clean_ip("2001:db8::1; rm -rf /") is None       # injection -> dropped
    assert discovery._clean_ip("not an ip") is None
    assert discovery._clean_ip("fe80::1") is None                     # link-local: un-onboardable, dropped


# --- the OpenWrt SSH transport (dhcp_leases_openwrt): a forced-command dnsmasq lease-file read -----------------
_SSH_CREDS = {"KONTROLL_OPENWRT_DISCOVERY_KEYFILE": "/run/user/1000/ow.key",
              "KONTROLL_OPENWRT_DISCOVERY_KNOWN_HOSTS": "/home/admin/.config/kontroll/openwrt_known_hosts"}


class _FakeSshFetchers:
    """Hermetic stand-in for the ssh transport (the OpenWrt rung): returns scripted lease-file TEXT for any ssh_read
    (or raises a scripted transport error), recording each call. Same 1-method surface as the real _Fetchers.ssh_read,
    so the sweep's ssh dispatch/parse runs with no openssh-client and no device."""
    def __init__(self, text="", raise_exc=None):
        self.text, self.raise_exc = text, raise_exc
        self.calls = []

    def ssh_read(self, host, user, keyfile, known_hosts):
        self.calls.append({"host": host, "user": user, "keyfile": keyfile, "known_hosts": known_hosts})
        if self.raise_exc:
            raise self.raise_exc
        return self.text


def _lease_text():
    """The dnsmasq lease-file golden (the shape _run_ssh/_parse_dnsmasq_leases see) — one string, dnsmasq columns."""
    return _fixture("openwrt_dhcp_leases.txt")


def test_parse_dnsmasq_leases_maps_columns():
    """_parse_dnsmasq_leases reads the dnsmasq lease-file golden -> {ip, mac, hostname} per lease: the IP is column 3,
    the MAC column 2 (already canonical colon-hex — no OID/hex-normalization like the SNMP path), and a `*` hostname
    (dnsmasq "no name") degrades to None; the expiry + client-id columns are ignored. Guards a column-order regression
    surfacing the wrong field as the IP + the `*`->None degrade."""
    assert discover._parse_dnsmasq_leases(_lease_text(), {}) == [
        {"ip": "192.0.2.50", "mac": "00:00:5e:00:53:01", "hostname": "printer"},
        {"ip": "192.0.2.51", "mac": "00:00:5e:00:53:02", "hostname": None},
        {"ip": "192.0.2.10", "mac": "00:00:5e:00:53:03", "hostname": "core-switch"}]


def test_parse_dnsmasq_leases_tolerates_hostile_text():
    """A device byte is DATA: a short line (<4 fields), a blank line, and a non-str input all yield NO row / [] (never
    a raise, never a phantom candidate). Guards a hostile/truncated lease file crashing the sweep."""
    text = "1720012345 00:00:5e:00:53:01 192.0.2.50 printer *\nshort\n\na b c"
    assert discover._parse_dnsmasq_leases(text, {}) == [
        {"ip": "192.0.2.50", "mac": "00:00:5e:00:53:01", "hostname": "printer"}]
    assert discover._parse_dnsmasq_leases(None, {}) == []


def test_offer_reaches_openwrt_via_declared_block():
    """The OpenWrt rung: the openwrt class OFFERS dhcp_leases_openwrt through its DECLARED `discovery:` block against
    its REAL module.yml. Guards the ssh method's offer wiring — a class opts in explicitly, so onboarding an AP never
    silently starts reading its lease file."""
    module = yaml.safe_load(open(os.path.join(ROOT, "modules", "openwrt", "module.yml"), encoding="utf-8"))
    methods_by = {m["name"]: m for m in catalog.load_discovery()}
    assert "dhcp_leases_openwrt" in [m["name"] for m in discover.offered_methods(module, methods_by)]


def _sweep_openwrt(fetchers, environ=None):
    """Run the sweep with openwrt enabled + one wireless_ap host, the REAL registry, an injected ssh fetcher."""
    return discover.sweep({"enabled_modules": ["openwrt"]},
                          {"wireless_ap": {"ap1": {"ansible_host": "192.0.2.3"}}},
                          {m["name"]: m for m in catalog.load_discovery()}, fetchers,
                          _SSH_CREDS if environ is None else environ)


def test_run_method_ssh_is_dispatched():
    """The third transport seam: a `transport: ssh` descriptor dispatches to _run_ssh (not http/snmp), reads via the
    ssh_read fetcher, and parses the lease_file shape. Guards the ssh transport being silently mis-routed."""
    status, rows = discover._run_method(catalog.discovery_method("dhcp_leases_openwrt"), "192.0.2.3",
                                        _FakeSshFetchers(text=_lease_text()), _SSH_CREDS)
    assert status == "ok" and {r["ip"] for r in rows} == {"192.0.2.50", "192.0.2.51", "192.0.2.10"}


def test_sweep_openwrt_ssh_end_to_end():
    """OpenWrt rung end-to-end (hermetic): enabling openwrt + one wireless_ap host makes the sweep resolve the DECLARED
    dhcp_leases_openwrt method, dispatch to the ssh transport (ssh_read with the operator-exported keyfile/known_hosts
    PATHS + user root), parse the dnsmasq lease file, and tag each lease. Guards the whole offer->ssh-transport->parse->
    assemble path AND that ssh_read receives user='root' + the two exported env PATHS (not values)."""
    fk = _FakeSshFetchers(text=_lease_text())
    result = _sweep_openwrt(fk)
    assert len(fk.calls) == 1 and fk.calls[0]["user"] == "root"
    assert fk.calls[0]["keyfile"] == _SSH_CREDS["KONTROLL_OPENWRT_DISCOVERY_KEYFILE"]
    assert fk.calls[0]["known_hosts"] == _SSH_CREDS["KONTROLL_OPENWRT_DISCOVERY_KNOWN_HOSTS"]
    assert result["sources"][0] == {"key": "openwrt", "method": "dhcp_leases_openwrt",
                                    "host": "192.0.2.3", "status": "ok", "count": 3}
    assert {l["ip"] for l in result["leases"]} == {"192.0.2.50", "192.0.2.51", "192.0.2.10"}
    assert all(l["source_key"] == "openwrt" and l["source_host"] == "192.0.2.3" for l in result["leases"])


def test_sweep_openwrt_no_creds_when_env_unset_and_never_reaches():
    """With EITHER env-PATH unset the source is `no-creds` (zero rows) AND ssh_read is NEVER called — fail-closed, no
    password/agent/TOFU fallback (the AP is never reached without BOTH the key + the pinned known_hosts). FAILCLOSED-1."""
    fk = _FakeSshFetchers(text=_lease_text())
    result = _sweep_openwrt(fk, environ={"KONTROLL_OPENWRT_DISCOVERY_KEYFILE": "/k"})   # known_hosts unset
    assert result["sources"][0]["status"] == "no-creds" and result["sources"][0]["count"] == 0
    assert result["leases"] == [] and fk.calls == []


def test_sweep_openwrt_unreachable_is_tolerated():
    """A transport failure (AP down / host-key mismatch / missing openssh-client -> FileNotFoundError, an OSError
    subclass) records `unreachable` with zero rows and the sweep completes — a down AP is tolerated, never fatal."""
    result = _sweep_openwrt(_FakeSshFetchers(raise_exc=OSError("ssh rc=255")))
    assert result["sources"][0]["status"] == "unreachable" and result["leases"] == []


def test_ssh_sweep_roundtrips_through_read_inbox(tmp_path, monkeypatch):
    """write_artifact(ssh sweep) then read_inbox(path): an OpenWrt lease candidate flows through the pure read side
    UNCHANGED — a v4 dnsmasq {ip,mac,hostname} row is exactly the shape service/discovery already reads, so read_inbox
    needs ZERO change for the ssh transport (the read-only pin stays green + untouched). Guards the two halves of the
    spine agreeing across the ssh transport, incl. a real hostname + MAC surviving normalization."""
    monkeypatch.setattr(discovery.fleet, "_inventory_groups", lambda: {})
    art = tmp_path / "inbox.json"
    discover.write_artifact(_sweep_openwrt(_FakeSshFetchers(text=_lease_text())), str(art))
    inbox = discovery.read_inbox(str(art))
    assert {c["ip"] for c in inbox["candidates"]} == {"192.0.2.50", "192.0.2.51", "192.0.2.10"}
    printer = next(c for c in inbox["candidates"] if c["ip"] == "192.0.2.50")
    assert printer["mac"] == "00:00:5e:00:53:01" and printer["hostname"] == "printer"


def _ssh_read_argv_list():
    """The ast.List literal bound to `argv` inside ssh_read, plus the Popen Call + the fn node. Asserts exactly one
    subprocess.Popen in ssh_read whose FIRST arg is EXACTLY the bare Name `argv` (not an inline +concat / helper /
    list() — those would let a tail hide), and that `argv` is bound exactly once to a BARE list literal. This is the
    idiom `snmp_walk` uses (`argv = [...]; Popen(argv, ...)`), so the twin resolves the binding, not an inline list."""
    import ast as _ast
    fn = _readonly_pins.find_function("scripts/kontroll-discover.py", "ssh_read")
    popens = [n for n in _ast.walk(fn)
              if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute) and n.func.attr == "Popen"]
    assert len(popens) == 1, "ssh_read must spawn exactly one subprocess.Popen, found %d" % len(popens)
    call = popens[0]
    assert call.args and isinstance(call.args[0], _ast.Name) and call.args[0].id == "argv", \
        "ssh_read's Popen must take the bare `argv` name as its first arg (no inline +concat / helper / list())"
    binds = [n for n in _ast.walk(fn)
             if isinstance(n, _ast.Assign) and any(isinstance(t, _ast.Name) and t.id == "argv" for t in n.targets)]
    assert len(binds) == 1, "argv must be bound exactly once (no rebinding), found %d" % len(binds)
    argv = binds[0].value
    assert isinstance(argv, _ast.List), \
        "ssh_read's argv must be a bare list literal (no +concat / helper / list()); got %s" % type(argv).__name__
    return fn, call, argv


def test_ssh_read_is_forced_command_read_only():
    """THE grep-can't-see-it backstop (OW-GATE): prove ssh_read runs a read-only, NO-client-command ssh whose remote
    command is FORCED on the AP's authorized_keys — a property a substring gate CANNOT express because a remote command
    is just a TRAILING argv word after `user@host`. Parses ssh_read's argv AST and asserts: (1) a single static-list
    Popen argv, unmutated (no +concat/helper/append/extend/insert/+=/slice-assign); (2) no `*`-splat element; (3) the
    LAST element is the host-token BinOp and NOTHING follows it; (4) every earlier element is a Constant string or a
    safe `%`-BinOp option (never a bare command Constant/Name); (5) shell=True never appears; (6) the fail-closed
    hardening options (BatchMode/StrictHostKeyChecking/IdentitiesOnly) are PRESENT (no soften-by-omission re-enabling
    TOFU/agent/password). Guards a future edit that appends a remote command, builds argv via +/append/extend/splat/
    helper, swaps in a variable command, or drops a hardening option — each of which a grep over source text misses."""
    import ast as _ast
    fn, call, argv = _ssh_read_argv_list()

    # (2) no *-splat can hide an arbitrary tail
    assert not any(isinstance(el, _ast.Starred) for el in argv.elts), "no *-splat allowed in ssh_read argv"

    # (1) `argv` is bound exactly once and never mutated (no append/extend/insert/+=/slice-assign) before Popen
    argv_binds = [n for n in _ast.walk(fn)
                  if isinstance(n, _ast.Assign) and any(isinstance(t, _ast.Name) and t.id == "argv" for t in n.targets)]
    assert len(argv_binds) == 1, "argv must be bound exactly once (no rebinding), found %d" % len(argv_binds)
    for n in _ast.walk(fn):
        if isinstance(n, _ast.AugAssign) and isinstance(n.target, _ast.Name) and n.target.id == "argv":
            raise AssertionError("argv must not be augmented (+=) — a trailing command could be smuggled")
        if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute) \
                and isinstance(n.func.value, _ast.Name) and n.func.value.id == "argv" \
                and n.func.attr in ("append", "extend", "insert"):
            raise AssertionError("argv.%s(...) forbidden — the argv must stay the static list literal" % n.func.attr)
        if isinstance(n, _ast.Assign):
            for t in n.targets:
                if isinstance(t, _ast.Subscript) and isinstance(t.value, _ast.Name) and t.value.id == "argv":
                    raise AssertionError("argv[...] assignment forbidden — no post-hoc mutation of the argv")

    # (3) the LAST element is the host-token `%`-BinOp, and nothing follows it
    last = argv.elts[-1]
    assert isinstance(last, _ast.BinOp) and isinstance(last.op, _ast.Mod) \
        and isinstance(last.left, _ast.Constant) and isinstance(last.left.value, str), \
        "ssh_read's LAST argv element must be the host-token BinOp (a user@host format string), got %s" % _ast.dump(last)
    assert last.left.value.strip() == "%s@%s", \
        "the host-token format must be exactly the user@host template (a space+command would smuggle a command); got %r" \
        % last.left.value

    # (4) every earlier element is a Constant str, a safe `%`-BinOp option, or the key-path Name that FOLLOWS `-i`
    #     (an identity-FILE argument — never a command; a bare Name anywhere else could hide a command word). The host
    #     is already pinned LAST (step 3), so nothing runs AFTER <user>@<host> regardless.
    const_vals, elts = set(), argv.elts
    for i, el in enumerate(elts[:-1]):
        if isinstance(el, _ast.Constant) and isinstance(el.value, str):
            const_vals.add(el.value)
            continue
        if isinstance(el, _ast.BinOp) and isinstance(el.op, _ast.Mod) \
                and isinstance(el.left, _ast.Constant) and isinstance(el.left.value, str) \
                and "@" not in el.left.value and " " not in el.left.value:
            continue                      # `UserKnownHostsFile=%s` / `ConnectTimeout=%d` — an option, not a command
        if isinstance(el, _ast.Name) and i > 0 \
                and isinstance(elts[i - 1], _ast.Constant) and elts[i - 1].value == "-i":
            continue                      # the identity-FILE path argument after `-i` (a key path, never a command)
        raise AssertionError("unexpected ssh_read argv element (a command word could hide here): %s" % _ast.dump(el))

    # (5) shell=True never appears on the Popen call
    for kw in call.keywords:
        if kw.arg == "shell":
            assert not (isinstance(kw.value, _ast.Constant) and kw.value.value), "Popen(shell=True) forbidden"

    # (6) the fail-closed hardening options are PRESENT as constants (a future edit can't soften-by-omission)
    for required in ("BatchMode=yes", "StrictHostKeyChecking=yes", "IdentitiesOnly=yes"):
        assert required in const_vals, \
            "ssh_read must keep the hardening option %r (fail-closed: no TOFU/agent/password fallback)" % required


def test_actor_has_no_shell_true_and_no_extra_ssh_spawn():
    """Actor-wide backstop (SPAWN-2): NO subprocess.* call in kontroll-discover.py passes shell=True, and the ONLY
    ssh/scp/sftp argv-spawn in the whole actor is ssh_read's — so a future edit can't add a second
    `subprocess.run(["ssh", …, host, cmd])` in another function that the ssh_read-scoped twin never inspects.
    Complements the grep's os.system/os.popen/pty/paramiko/os.exec ban. Guards an arbitrary-remote-command SSH being
    introduced elsewhere in the actor."""
    import ast as _ast
    tree = _ast.parse(open(os.path.join(ROOT, "scripts", "kontroll-discover.py"), encoding="utf-8").read())
    # (a) NO subprocess.* call passes shell=True anywhere in the actor
    for n in _ast.walk(tree):
        if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute) \
                and n.func.attr in ("Popen", "run", "call", "check_output", "check_call"):
            for kw in n.keywords:
                if kw.arg == "shell":
                    assert not (isinstance(kw.value, _ast.Constant) and kw.value.value), \
                        "shell=True forbidden in the discovery actor"
    # (b) EXACTLY ONE ssh/scp/sftp argv-list literal exists in the whole actor (ssh_read's, whether inline or bound
    #     to `argv`) — a second one (a new function's `["ssh", …, host, cmd]`) would evade the ssh_read-scoped twin
    ssh_lists = [n for n in _ast.walk(tree)
                 if isinstance(n, _ast.List) and n.elts and isinstance(n.elts[0], _ast.Constant)
                 and n.elts[0].value in ("ssh", "scp", "sftp")]
    assert len(ssh_lists) == 1, \
        "exactly one ssh-family argv list (ssh_read's) allowed in the actor, found %d" % len(ssh_lists)


def test_ssh_read_byte_caps_a_flooding_device(monkeypatch):
    """S-CAP (the security-critical bound the fake fetcher can't prove): the REAL _Fetchers.ssh_read STREAMS the child
    pipe and caps the PARENT read at MAX_BYTES, KILLING a device that streams past it, so a hostile/huge lease file
    can't OOM the control node. Drives the real read/cap/kill loop against a scripted oversized stream (no openssh, no
    device) by faking subprocess.Popen. ssh_read returns a STRING (not lines), so the cap is asserted on len(result).
    Guards a regression to capture_output=True (which would buffer the whole child stdout before any cap applies)."""
    monkeypatch.setattr(discover, "MAX_BYTES", 64)
    state = {"killed": 0}

    class _FakeStdout:
        def read(self, n):
            return b"A" * n            # the child "streams" exactly what is asked — the cap is the read arg, not this

    class _FakeProc:
        returncode = None

        def __init__(self):
            self.stdout = _FakeStdout()

        def poll(self):
            return None                # still running -> forces the kill path

        def kill(self):
            state["killed"] += 1
            self.returncode = -9

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(discover.subprocess, "Popen", lambda *a, **k: _FakeProc())
    text = discover._Fetchers().ssh_read("192.0.2.3", "root", "/k", "/kh")
    assert state["killed"] >= 1                                    # the streaming child was killed
    assert len(text) <= discover.MAX_BYTES                         # the parent buffered at most the cap


def test_snmp_walk_byte_caps_a_flooding_device(monkeypatch):
    """S-CAP — the security-critical bound the fake fetcher can't prove: the REAL _Fetchers.snmp_walk STREAMS the
    child pipe and caps the PARENT read at MAX_BYTES, KILLING a device that streams past it, so a hostile/huge ARP
    table can't OOM the control VM. Drives the real read/cap/kill loop against a scripted oversized stream (no
    net-snmp, no device) by faking subprocess.Popen. Guards a regression to capture_output=True (which would buffer
    the whole child stdout before any cap could apply)."""
    monkeypatch.setattr(discover, "MAX_BYTES", 64)
    monkeypatch.setattr(discover, "MAX_WALK_LINES", 3)
    state = {"killed": 0}

    class _FakeStdout:
        def read(self, n):
            return b"A" * n            # the child "streams" exactly what is asked — the cap is the read arg, not this

    class _FakeProc:
        returncode = None
        def __init__(self):
            self.stdout = _FakeStdout()
        def poll(self):
            return None                # still running -> forces the kill path
        def kill(self):
            state["killed"] += 1
            self.returncode = -9
        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(discover.subprocess, "Popen", lambda *a, **k: _FakeProc())
    lines = discover._Fetchers().snmp_walk("192.0.2.2", "1.3.6.1.2.1.4.22.1.2", "u", "a", "p")
    assert state["killed"] >= 1                                    # the streaming child was killed
    assert sum(len(l) for l in lines) <= discover.MAX_BYTES        # the parent buffered at most the cap
    assert len(lines) <= discover.MAX_WALK_LINES                   # and the line cap applied


def test_snmp_sweep_roundtrips_through_read_inbox(tmp_path, monkeypatch):
    """write_artifact(snmp sweep) then read_inbox(path): an ARP candidate (hostname None) flows through the pure read
    side UNCHANGED — the IP is the key, a null hostname is not a drop reason — so read_inbox needs ZERO change for the
    SNMP transport (the read-only pin stays green + untouched). Guards the two halves of the spine agreeing across a
    non-HTTP transport."""
    monkeypatch.setattr(discovery.fleet, "_inventory_groups", lambda: {})
    art = tmp_path / "inbox.json"
    discover.write_artifact(_sweep_cisco(_FakeSnmpFetchers(lines=_arp_lines())), str(art))
    inbox = discovery.read_inbox(str(art))
    assert {c["ip"] for c in inbox["candidates"]} == {"192.0.2.50", "192.0.2.51", "192.0.2.10"}
    assert all(c["hostname"] is None for c in inbox["candidates"])


def test_sweep_flood_cap_bounds_rows(monkeypatch):
    """A malicious/huge lease response is COUNT-capped at MAX_ROWS before it reaches the artifact (the flood cap's
    parse half; the byte cap is the socket half). Guards a device flooding the inbox / OOMing a downstream reader."""
    monkeypatch.setattr(discover, "MAX_ROWS", 3)
    body = json.dumps({"rows": [{"address": "192.0.2.%d" % i, "hwaddr": "00:00:5e:00:53:00", "hostname": "h"}
                                for i in range(2, 12)]})
    result = _sweep_opnsense(_FakeFetchers(body=body))
    assert result["sources"][0]["count"] == 3 and len(result["leases"]) == 3


def test_sweep_artifact_roundtrips_through_read_inbox(tmp_path, monkeypatch):
    """write_artifact(...) then read_inbox(path) round-trips: the writer (top-level actor) and the reader (pure
    service) agree on the path/schema. Guards a silent divergence between the two halves of the read spine."""
    monkeypatch.setattr(discovery.fleet, "_inventory_groups", lambda: {})   # nothing onboarded
    art = tmp_path / "inbox.json"
    discover.write_artifact(_sweep_opnsense(_FakeFetchers(body=_fixture_body())), str(art))
    inbox = discovery.read_inbox(str(art))
    assert {c["ip"] for c in inbox["candidates"]} == {"192.0.2.50", "192.0.2.51", "192.0.2.10"}


# --- the pure read_inbox (IP-diff + normalization + read-only pin) --------------------------------------------
def _write_artifact(tmp_path, leases, sources=None):
    art = tmp_path / "inbox.json"
    art.write_text(json.dumps({"schema": 1, "generated_at": "2026-07-03T00:00:00+00:00",
                               "sources": sources or [], "leases": leases}), encoding="utf-8")
    return str(art)


def test_read_inbox_hides_onboarded_ips(tmp_path, monkeypatch):
    """A lease whose IP is already an onboarded `ansible_host` is HIDDEN (the IP-diff); the rest surface, sorted +
    de-duped. Guards the inbox re-suggesting a host the operator already onboarded."""
    monkeypatch.setattr(discovery.fleet, "_inventory_groups",
                        lambda: {"edge_firewall": {"fw": {"ansible_host": "192.0.2.10"}}})
    leases = [{"ip": "192.0.2.50", "mac": "00:00:5e:00:53:01", "hostname": "printer"},
              {"ip": "192.0.2.10", "mac": "00:00:5e:00:53:03", "hostname": "core-switch"},
              {"ip": "192.0.2.50", "mac": "00:00:5e:00:53:01", "hostname": "printer"}]   # dup of .50
    inbox = discovery.read_inbox(_write_artifact(tmp_path, leases))
    assert [c["ip"] for c in inbox["candidates"]] == ["192.0.2.50"]   # .10 onboarded-hidden, .50 de-duped


def test_read_inbox_normalizes_hostile_fields(tmp_path, monkeypatch):
    """A device byte is DATA, not code: a lease with an injection IP is DROPPED (IP is the key); a lease with a
    valid IP but a `<script>` hostname / a junk MAC keeps the row with those fields degraded to None. Guards an
    un-normalized device value reaching the GUI."""
    monkeypatch.setattr(discovery.fleet, "_inventory_groups", lambda: {})
    leases = [{"ip": "192.0.2.60; rm -rf /", "mac": "x", "hostname": "y"},          # bad IP -> dropped
              {"ip": "192.0.2.61", "mac": "zz:zz", "hostname": "<script>alert(1)"}]  # bad mac/hostname -> None
    inbox = discovery.read_inbox(_write_artifact(tmp_path, leases))
    assert [c["ip"] for c in inbox["candidates"]] == ["192.0.2.61"]
    only = inbox["candidates"][0]
    assert only["mac"] is None and only["hostname"] is None


def test_read_inbox_missing_artifact_is_empty(monkeypatch):
    """A missing sweep artifact yields an empty inbox, never a raise — the GUI worker survives a never-run sweep."""
    monkeypatch.setattr(discovery.fleet, "_inventory_groups", lambda: {})
    assert discovery.read_inbox(os.path.join(ROOT, "does", "not", "exist.json")) == \
        {"generated_at": None, "sources": [], "candidates": []}


def test_read_inbox_sanitizes_source_provenance(tmp_path, monkeypatch):
    """The per-source provenance rows are coerced to safe shapes (token key/method/status, validated host IP,
    non-negative int count) so even a tampered artifact can't push a metacharacter through the provenance panel."""
    monkeypatch.setattr(discovery.fleet, "_inventory_groups", lambda: {})
    sources = [{"key": "opnsense", "method": "dhcp_leases_opnsense", "host": "192.0.2.1",
                "status": "ok", "count": 3},
               {"key": "bad key!", "method": "x", "host": "not-an-ip", "status": "ok", "count": -5}]
    inbox = discovery.read_inbox(_write_artifact(tmp_path, [], sources))
    assert inbox["sources"][0] == {"key": "opnsense", "method": "dhcp_leases_opnsense",
                                   "host": "192.0.2.1", "status": "ok", "count": 3}
    assert inbox["sources"][1]["key"] is None and inbox["sources"][1]["host"] is None \
        and inbox["sources"][1]["count"] == 0


# --- OUI vendor enrichment (Rung 3): a pure read-side display hint, longest-prefix match -----------------------
def test_vendor_for_longest_prefix_match(monkeypatch):
    """`_vendor_for` does LONGEST-prefix match (36→28→24 = probe 9→7→6 nibbles): with a lookup carrying all three
    lengths for the same OUI, a MAC resolves to the 9-nibble (MA-S) vendor, proving longest-wins. WHY: guards the
    MA-M/MA-S correctness bug — IEEE re-parcels a /24, so a naive first-3-bytes lookup returns the wrong owner."""
    monkeypatch.setattr(discovery, "_OUI", {"00005e": "IANA-L", "00005e0": "IANA-M", "00005e005": "IANA-S"})
    assert discovery._vendor_for("00:00:5e:00:53:01") == "IANA-S"        # 9-nibble (00005e005) wins


def test_vendor_for_falls_back_to_shorter_prefix_and_degrades(monkeypatch):
    """A MAC whose 9- and 7-nibble prefixes miss but the 6-nibble hits resolves to the /24 vendor (the fallback);
    a None mac and an unknown OUI both degrade to None (a graceful 'unknown vendor', never a raise). WHY: guards the
    fallback path + the advisory degrade-to-None contract (mac/hostname already have it)."""
    monkeypatch.setattr(discovery, "_OUI", {"00005e": "IANA-L"})         # only the 6-nibble MA-L key is present
    assert discovery._vendor_for("00:00:5e:00:53:07") == "IANA-L"        # 9/7-nibble miss -> the /24 fallback hits
    assert discovery._vendor_for(None) is None                           # a degraded mac -> None vendor
    assert discovery._vendor_for("ff:ff:ff:ff:ff:ff") is None            # OUI absent -> None (broadcast, doc-allowlisted)


def test_read_inbox_enriches_vendor(tmp_path, monkeypatch):
    """read_inbox annotates each candidate with the IEEE OUI `vendor` hint (longest-prefix on the normalized MAC); a
    candidate whose MAC degraded to None carries vendor None. WHY: guards the widened candidate schema + the
    None-degrade. `_OUI` is monkeypatched to a tiny doc-safe lookup (the real ~2 MB artifact isn't needed to prove the
    read-path wiring)."""
    monkeypatch.setattr(discovery.fleet, "_inventory_groups", lambda: {})
    monkeypatch.setattr(discovery, "_OUI", {"00005e": "IANA"})
    leases = [{"ip": "192.0.2.50", "mac": "00:00:5e:00:53:01", "hostname": "printer"},
              {"ip": "192.0.2.51", "mac": "zz", "hostname": "h"}]        # bad mac -> None -> vendor None
    by_ip = {c["ip"]: c for c in discovery.read_inbox(_write_artifact(tmp_path, leases))["candidates"]}
    assert by_ip["192.0.2.50"]["vendor"] == "IANA"
    assert by_ip["192.0.2.51"]["mac"] is None and by_ip["192.0.2.51"]["vendor"] is None


def test_load_oui_reads_the_committed_pin():
    """The committed oui/oui-lookup.generated.json loads via `_load_oui` (a non-empty dict) and carries the RFC-7042
    doc OUI `00:00:5e` (IANA's real MA-L). WHY: a smoke test that the pinned artifact + the ROOT-bound `paths.OUI_DIR`
    wire up — the enrichment is silently dead if the artifact isn't baked/loadable (the discovery bake-gap class of
    bug, live-caught on the 2026-07-03 baked-box dogfood)."""
    oui = discovery._load_oui()
    assert isinstance(oui, dict) and oui, "the committed OUI lookup must load as a non-empty dict"
    assert "00005e" in oui, "the IANA doc OUI 00:00:5e must be present in the pinned registry"


def test_read_inbox_is_read_only_by_construction():
    """THE load-bearing pin: `read_inbox` calls NO write/actuation verb and opens no file for writing — the read
    view can never gain a write/promote/actuate path (the zero-new-authority proof, SECURITY C18). Still green after
    the OUI enrichment (`_load_oui`/`_vendor_for` are pure committed-file READS). The completeness gate
    (test_readonly_completeness) additionally scans the whole module for any unregistered direct mutator."""
    _readonly_pins.assert_read_only("scripts/kontroll/service/discovery.py", "read_inbox")


def test_discover_audit_path_defaults_under_write_root(monkeypatch, tmp_path):
    """The sweep's audit-log DEFAULT resolves under `paths.write_root()`, NOT the baked-read-only `ROOT` — so on a
    Phase-B baked deploy the per-source audit write lands in the writable propose clone instead of raising
    PermissionError on `/opt/kontroll/local` and aborting the WHOLE sweep (live-caught on the baked-box discovery
    dogfood 2026-07-03; mirrors `inbox_path()`). Identity holds when write_root()==ROOT (the legacy /repo deploy),
    and the two env overrides still take precedence over the default."""
    monkeypatch.delenv("KONTROLL_DISCOVER_AUDIT_LOG", raising=False)
    monkeypatch.delenv("KONTROLL_API_AUDIT_LOG", raising=False)
    monkeypatch.setattr(discover.paths, "write_root", lambda: str(tmp_path))
    assert discover._audit_path() == os.path.join(str(tmp_path), "local", "discover-audit.log")
    monkeypatch.setenv("KONTROLL_DISCOVER_AUDIT_LOG", "/tmp/override.log")   # first branch wins over the default
    assert discover._audit_path() == "/tmp/override.log"


def test_the_sweep_skips_a_fleet_line_that_is_not_a_class_key():
    """`sweep()` over a fleet whose `enabled_modules` holds `null`, a number and a name outside the charset
    returns an artifact (no source, no lease) instead of raising out of the whole run. Guards the last reader that
    handed a raw fleet entry to `module_file()`: one bad line in the fleet file blanked discovery for every
    class."""
    result = discover.sweep({"enabled_modules": [None, 123, "../x", "Upper"]}, {}, {}, {}, {})
    assert result["sources"] == [] and result["leases"] == []


def test_a_lease_named_like_an_impossible_date_does_not_blank_the_inbox():
    """A device-supplied host name the YAML reader resolves and cannot build — `2026-02-30`, `0000-00-00` — is
    cleaned to "no host name" like any other name that is not a plain host name; `_clean_hostname` returns None
    and does not raise. The name is chosen by whoever asks a lease-serving device for an address, and one such
    lease made the whole Discovery panel answer 500: the hostname validator had gained a YAML check, and the
    check raised."""
    from kontroll.service import discovery as discovery_service
    for name in ("2026-02-30", "0000-00-00", "9999-99-99", "null", "123"):
        assert discovery_service._clean_hostname(name) is None, name
    assert discovery_service._clean_hostname("printer-3") == "printer-3"
