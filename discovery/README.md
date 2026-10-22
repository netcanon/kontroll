# `discovery/` — the passive-discovery method registry (the discovery inbox)

Drop-in descriptors that tell the top-level sweep actor `scripts/kontroll-discover.py` **how to read an
already-onboarded device's OWN lease / neighbour view read-only**, so the un-onboarded hosts on the network can be
surfaced into an onboard *inbox* — a human still onboards + promotes every one (C10 unchanged; discovery only
**pre-fills the existing onboard form**, it never auto-onboards). Peer of [`telemetry/`](../telemetry/) and
[`logging/`](../logging/) — adding a discovery source is **one new file here**, never an edit to a hub.

**Passive by construction, read-only, never a scan.** The sweep GETs the lease table a firewall/router already
keeps; it never enumerates the network (no ICMP/ARP sweep, no CIDR expansion — it reaches only the onboarded device
address). A discovered lease is **untrusted DATA, not code**. Full trust boundary: [SECURITY.md
C18](../SECURITY.md#c18--passive-discovery-is-a-read-only-cred-reusing-untrusted-response-surface).

## The two-part read spine

| Part | File | Layer | What |
|---|---|---|---|
| the **sweep** (writes) | `scripts/kontroll-discover.py` | **top-level actor** (outside the pinned service layer, like `kontroll-autopromoter`) | reaches each onboarded class that offers a method, GETs read-only (byte-capped), parses via a `parse.shape`-keyed `_PARSERS` table, count-caps, writes the git-ignored `local/discovery-inbox.generated.json` |
| the **read** (pure) | `scripts/kontroll/service/discovery.py` | pinned service layer | `read_inbox()` loads that artifact + the onboarded inventory, normalizes every field, hides already-onboarded IPs, returns the inbox — **zero** write verbs, `assert_read_only`-pinned |

The sweep's write lives in the top-level actor **on purpose**: an `open(cache, 'w')` inside a `service/*.py`
module trips the read-only completeness gate (`tests/unit/_readonly_pins.py`), so keeping the write out of the
service layer is what lets `read_inbox` stay provably read-only (the zero-new-authority proof this feature rests on).

## Schema (one `discovery/<method>.yml` per source)

| Field | Meaning |
|---|---|
| `name` | registry key == filename stem == the `- {method: <name>}` reference in a module's `discovery:` block |
| `label` / `order` | human label / `load_discovery()` sort order |
| `source.transport` | OPTIONAL — `http` (default), `snmp`, or `ssh`. The sweep dispatches this NAME to a read-only fetcher (`http_get` / `snmp_walk` / `ssh_read`); an absent key = `http`, so the DHCP JSON methods are unchanged. Passivity is structural: ALL THREE primitives are read-only, machine-enforced by the `discovery-passive-only` grep-gate (a negative leg + a case-sensitive POSITIVE leg for ssh) + the `ssh_read` argv-AST twin |
| `source.endpoint` | (http) the read-only GET URL; `${DEVICE_ADDR}` is templated per host (mgmt-routed) |
| `source.walk_oid` | (snmp) the OID column to `snmpbulkwalk` read-only (e.g. ipNetToMediaPhysAddress `1.3.6.1.2.1.4.22.1.2`); host is passed positionally (`:161`), NOT a URL |
| `source.user` | (ssh) the account the forced-command key is authorized for (OpenWrt: `root`); host is passed positionally (`<user>@<host>`), NOT a URL. There is NO client-sent path — the read target is fixed by the AP's forced command |
| `source.auth` | http: `{strategy: basic\|custom, value}` (`${ENV}` refs). snmp: `{strategy: snmp_v3, user_env/auth_pass_env/priv_pass_env, auth_proto, priv_proto}`. ssh: `{strategy: ssh_forced_command, keyfile_env, known_hosts_env}` — env NAMES that hold FILE PATHS (the decrypted forced-command key + the operator-pinned known_hosts). In every case the secret enters ONLY at the fetcher boundary (list argv, never shell-interpolated) |
| `source.verify` | OPTIONAL (http) — `false` reaches a self-signed device read-only (deriving the posture from the class `vendor_defaults` is a later rung) |
| `parse.shape` | the `_PARSERS` table key — a **NAME** (`json_rows` / `arp_table` / `neighbors_table` / `lease_file`), never a vendor literal; no parser function knows the word "opnsense"/"cisco"/"openwrt" |
| `parse.rows` / `parse.fields` | for `json_rows`: the array field in the body, and the `{ip, mac, hostname}` → response-field map (`arp_table`/`neighbors_table`/`lease_file` read neither — the columns are intrinsic to the format) |
| `wiring_role` | OPTIONAL — the device-side role that authorizes the read cred (ssh: `openwrt_discovery_key` drops the forced-command pubkey on the AP) |
| `secret_domain` | OPTIONAL SOPS domain a credentialed read reuses **as-scoped** (never widened), audited by NAME only (C12) |
| `secret_env_map` | OPTIONAL — how the `${ENV}` creds compose from the domain's SOPS field NAMES (value-free; the same block telemetry/logging use) |
| `applies_when` | OPTIONAL facts predicate (mostly ABSENT — a class opts in via a declared `discovery:` block, see below) |
| `access_chain` | the read's access-chain header as data (`chain`/`may_break`/`fallback`/`blast_radius`) |

## The offer — a **declared** block, not a facts predicate

A device-class opts into a discovery method by declaring it in its `modules/<key>/module.yml`:

```yaml
discovery:
  - {method: dhcp_leases_opnsense}
```

This is **declared, not facts-derived**, on purpose: `ansibleguy.opnsense` ships **no** connection plugin (its
`facts.pinned.yml` is `plugins: {}`; the class declares `raw_ssh`), so an `applies_when: {plugin: httpapi}` predicate
would offer to **nothing**. The declared block is the offer path — exactly like `logging/proxmox_api.yml` (declared
on the proxmox class). Pinned by `tests/unit/test_discovery.py::test_offer_reaches_opnsense`, which feeds the **real**
`facts.pinned.yml`.

## The shipped methods

(The set is pinned by `tests/unit/test_discovery.py::test_registry_loads_and_sorts_by_order` — a count in prose
would rot; the test fails loudly instead.)

| Method | source | direction | typical class |
|---|---|---|---|
| `dhcp_leases_opnsense` | `http` GET `/api/dhcpv4/leases/searchLease` (Basic) | pull (read-only) | an OPNsense edge firewall (declared; reuses SOPS `network`) |
| `dhcp_leases_fortigate` | `http` GET `/api/v2/monitor/system/dhcp` (Bearer) | pull (read-only) | a FortiGate edge firewall (the LIVE pre-cutover edge; `results`-keyed body) |
| `dhcp_leases_routeros` | `http` GET `/rest/ip/dhcp-server/lease` (Basic) | pull (read-only) | a MikroTik RouterOS core (staged; a **bare-array** body, no wrapper key) |
| `arp_neighbors_snmp` | `snmp` WALK ipNetToMediaTable `1.3.6.1.2.1.4.22.1.2` (SNMPv3 authPriv) | pull (read-only) | a Cisco IOS core switch (the SECOND transport; surfaces static/cross-VLAN neighbours DHCP can't) |
| `neighbors_snmp` | `snmp` WALK **dual-stack** ipNetToPhysicalTable `1.3.6.1.2.1.4.35.1.4` (SNMPv3 authPriv) | pull (read-only) | a Cisco IOS core switch (RFC-4293; the `neighbors_table` parse shape decodes a TYPE-tagged/LENGTH-prefixed index → v4 **or v6** — catches IPv6 ND neighbours the v4 ARP table + DHCP APIs never see). **SEE-ONLY**: a v6 candidate surfaces + pre-fills, but onboard's `ansible_host` stays `type: ipv4` — onboard a v6 host only once that knob is widened |
| `dhcp_leases_openwrt` | `ssh` forced-command `cat /tmp/dhcp.leases` (a DEDICATED read key) | pull (read-only) | an OpenWrt AP (the THIRD transport; OpenWrt has no read-only lease GET — ubus/LuCI are HTTP POST — so the forced-command SSH `cat` of the dnsmasq lease file is the tightest read; `lease_file` parse shape. The `openwrt_discovery_key` role authorizes the read pubkey on the AP) |

## Extending — add a discovery source

1. Drop a `discovery/<method>.yml` here (copy the nearest method as the template). If it's a JSON lease/neighbour
   API, reuse `parse.shape: json_rows` with your own `rows`/`fields` — **no code change**. The two vendor SIBLINGS
   of `dhcp_leases_opnsense` are the worked proof: `dhcp_leases_fortigate` (a `results`-keyed body + a `custom`
   `Bearer` token) and `dhcp_leases_routeros` (a **bare** top-level array, no wrapper key — omit `rows:`) each shipped
   as a pure file-only drop-in, ZERO change to `scripts/kontroll-discover.py`. A genuinely new response SHAPE or a new
   TRANSPORT adds code ONCE, keyed by a NAME (never a vendor branch): `arp_neighbors_snmp` (Rung 3, `source.transport:
   snmp` + the `snmp_walk` fetcher + the `arp_table` shape) and `dhcp_leases_openwrt` (the OpenWrt rung,
   `source.transport: ssh` + the read-only forced-command `ssh_read` fetcher + the `lease_file` shape) are the worked
   proofs; every FUTURE SNMP or JSON-lease or ssh method is then a pure file-only drop-in on top of those seams.
2. A device-class opts in by declaring `discovery: [{method: <method>}]` in its `modules/<key>/module.yml`.
3. **Export the read cred, then run the sweep on the control VM** (on-demand, **never** in `validate`/bootstrap — a
   live reach is not a hermetic gate, the BRICK-1 discipline). Discovery creds are **operator-exported at invocation**
   (there is no `secret_env_map` on the descriptors — `gen-secret-env` renders `.env` from `metrics:`/`logs:` only,
   never `discovery:`, and the SOPS field name is **instance-generated by onboard**: `<ns>_<coll>_<N>_<field>`, e.g.
   the live FortiGate token is `fortinet_fortios_1_api_token`, so a shipped descriptor cannot hardcode it). Decrypt
   your domain and export the `${ENV}` names the descriptor's `source.auth` uses, then run — e.g. for the FortiGate:
   ```bash
   export KONTROLL_FORTIGATE_DISCOVERY_TOKEN="$(sops -d instance/secrets/network.sops.yml | yq -r .fortinet_fortios_1_api_token)"
   python3 scripts/kontroll-discover.py --class fortigate   # writes local/discovery-inbox.generated.json
   ```
   The GUI inbox reads the artifact via `service/discovery.read_inbox()`. **Device prerequisites** are the device's,
   not kontroll's: the reused API token must permit the read AND (FortiGate) the control node must be in the API
   admin's **trusthost** (else 403) — dogfood-confirmed against FortiOS v7.2.13 (the `system/dhcp` shape is exact).

   For the **`ssh` transport** (`dhcp_leases_openwrt`) the exported env vars are FILE PATHS, not cred strings — the
   sweep runs `ssh -i <keyfile> … <user>@<host>` with NO client command, so the AP's `authorized_keys` forced command
   (`cat /tmp/dhcp.leases`) is all that runs. The provisioning tail is one-time + out-of-band (there is deliberately NO
   deploy-time provisioning — the sweep is on-demand, and a baked key would rot + can't be operator-pinned):

   ```bash
   # ONE-TIME (out-of-band): mint a DEDICATED forced-command keypair (never the ansible root key), authorize its
   # PUBLIC half on every AP via the wiring role, pin the AP host keys, and store the PRIVATE half in `network`.
   ssh-keygen -t ed25519 -f openwrt_discovery -N ''
   ansible-playbook -i instance/inventory --limit wireless_ap \
     -e openwrt_discovery_pubkey="$(cat openwrt_discovery.pub)" ansible/playbooks/... --check --diff   # then apply
   ssh-keyscan <ap-ip> > ~/.config/kontroll/openwrt_known_hosts     # operator-reviewed pin (no TOFU at sweep time)
   sops instance/secrets/network.sops.yml                            # add: openwrt_discovery_ssh_private_key: | <PEM>

   # AT SWEEP TIME (on the machine that runs the sweep): decrypt the key to an EPHEMERAL 0600 file, export both PATHS.
   umask 077
   sops -d instance/secrets/network.sops.yml | yq -r .openwrt_discovery_ssh_private_key > /run/user/$(id -u)/ow_disc.key
   export KONTROLL_OPENWRT_DISCOVERY_KEYFILE=/run/user/$(id -u)/ow_disc.key
   export KONTROLL_OPENWRT_DISCOVERY_KNOWN_HOSTS=$HOME/.config/kontroll/openwrt_known_hosts
   python3 scripts/kontroll-discover.py --class openwrt
   shred -u /run/user/$(id -u)/ow_disc.key                           # ephemeral — never persist the decrypted key
   ```

   `/run/user/$UID` (tmpfs, 0700) is the natural ephemeral home (the C12 onboard-key precedent). Either env-PATH unset
   ⇒ the source is `no-creds`, skipped. On a **baked deploy** the sweep runs `docker exec … onboard-gui python3
   /opt/kontroll/scripts/kontroll-discover.py`, so the key + known_hosts files must be reachable INSIDE that container
   (decrypt them under the writable `/propose` tree or a bind-mount, then export those in-container paths).
4. Keep committed fixtures/goldens on **documentation** identifiers only (RFC-5737 IPs `192.0.2.0/24` etc., RFC-7042
   MACs `00:00:5e:00:53:xx`) — the `discovery-p1-fixtures` grep-gate in `tests/validate.sh` fails on a live one (P-1).

## The GUI inbox (Rung 1b)

The operator surface is the read-only **Discovery** panel (`GET /api/discovery` → `service/discovery.read_inbox`,
mirroring `/api/pending`): one row per un-onboarded candidate, every device byte painted via `ET`/`textContent`
(a lease is attacker-influenceable — a `<script>` hostname renders as literal text). Each row also carries an advisory
**vendor hint** — the manufacturer looked up from the MAC's IEEE OUI (the pinned [`oui/`](../oui/) registry,
`service/discovery._vendor_for`, longest-prefix 36→28→24). It is a **DISPLAY HINT, never a class guess** (C18 F8): it
helps the human recognize *which* class to search for, but no code reads it — the vendor never enters the onboard flow
(an e2e asserts "Onboard this" pre-fills only the host, with no class pre-picked). A row's **"Onboard this"**
stashes the discovered host and pre-fills it into the EXISTING onboard form's host field — the operator searches +
picks the device class (Rung 1 doesn't guess it), then onboards + promotes (C10; **no auto-onboard**). The
`discovery-*` testids are in [tests/testid_reference.md](../tests/testid_reference.md); the e2e is
`tests/e2e/test_discovery_flow.py`.

## See also

- [`telemetry/`](../telemetry/) + [`logging/`](../logging/) — the sibling capability registries this mirrors.
- [SECURITY.md C18](../SECURITY.md#c18--passive-discovery-is-a-read-only-cred-reusing-untrusted-response-surface) — the trust boundary + accepted risk R-DISC-1.
- `docs/reviews/2026-07-03-discovery-inbox-scope/99-synthesis.md` — the design-of-record (blackboard, both reviewers GO-WITH-FIXES).
