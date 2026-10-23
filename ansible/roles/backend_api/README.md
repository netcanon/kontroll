# role: backend_api

**Execution backend**, not a device class. Generic `check`/`backup` for REST/API
devices **and services** (FortiGate, OPNsense, the Servarr apps, …), driven by a
per-API **recipe** (`ansible/backends/api/recipes/<name>.yml`) — auth + endpoints
as data. Uses plain `ansible.builtin.uri` delegated to localhost: **no vendor
module, no httpapi plugin, no collection required**. One role, N recipes — this is
how the API tail folds into data instead of a role per vendor (capability-matrix §5.2).

The generalization of the proven `roles/fortigate` capture: `fortigate` is now a
thin wrapper that points here with `backend_api_recipe: fortios`. Verified live —
the generic capture is byte-for-byte the FortiGate full-configuration (differing
only in FortiOS's per-export re-encrypted secret blobs).

## Entrypoints (`tasks_from`)

| Entrypoint | Purpose | Blast radius |
|---|---|---|
| `check`  | liveness via the recipe's `check.path` (read-only GET) | none |
| `backup` | capture via the recipe's `backup` endpoint, writes the body | none (read-only) |

## Params (set by the device class)
- `backend_api_recipe` — **required**: the recipe name under `backends/api/recipes/`.
- `backend_backup_label` — filename prefix fallback (recipe `backup.label` wins).

## Recipe schema (`backends/api/recipes/<name>.yml`)
Pure data, secret-free — the token is **named** (`auth.token_var`) and resolved from
the host's SOPS-backed vars at runtime, never inlined.

### Which variable actually holds the token (read this before adding a class)
**Two names are accepted, in order:** the recipe's declared `auth.token_var`, then the
per-host convention `<inventory_hostname with - replaced by _>_api_token`.

That is not belt-and-braces — it is a correction. `backends/api/backend.yml` used to claim
the planner resolved the recipe's `token_var` into the drop-in, and nothing ever did:
`service/onboard.py` puts only `network_os` into `backend_params`, so its
`params.get("token_var")` is always `None` and every onboarded api host gets the
convention name instead. `paths.py` even defines `RECIPES_DIR` that no Python reads. The
result was a role looking up `fortios_api_token` while the drop-in defined
`fortigate_100e_api_token` — **so the edge-firewall check could never authenticate**, and
because the resolve task is `no_log`, the undefined-variable fatal arrived *censored* and
`ping.yml`'s rescue reported a healthy firewall as a bare `UNREACHABLE`.

Both names are now tried, an unresolvable or **empty** token is refused up front naming
both candidates, and the liveness result is actually evaluated (it previously used
`failed_when: false` and was never read again, so the check could not fail at all).
Pinned by [`tests/unit/test_backend_api_check_teeth.py`](../../../tests/unit/test_backend_api_check_teeth.py).

```yaml
name: fortios
port: 443
validate_certs: false
auth:   {type: bearer, token_var: fortios_api_token}   # bearer | basic | apikey_header
check:  {method: GET, path: /api/v2/monitor/system/status}
backup: {method: GET, path: /api/.../config/backup, extract: body,
         label: FortiGate, suffix: full-configuration.conf}
```

Auth types: `bearer` (`Authorization: Bearer <token>`), `apikey_header`
(`<header_name>: <token>`), `basic` (`username_var`/`password_var`). Backup
endpoints must be **read-only** (GET / export). Output is `no_log` — the request
carries the token, the response the secret-bearing config.

**Two ports, two owners (S28).** The recipe's `port:` governs the check and backup
requests. The reachability probe that runs *before* them (`ping.yml`) reads the host's
`kontroll_probe_port`, which the onboard writes from the backend descriptor's `probe_port`
(443, the recipes' default) — an api host used to be probed on the SSH port and read
`UNREACHABLE` with its check never run. A recipe on another port needs its hosts to set
`ansible_port` or `mgmt_port`, which the probe reads first; binding a recipe to a host is
S22's channel. A host onboarded before S28 has no `kontroll_probe_port` until it is
re-onboarded, and is probed on 22 as before.

## See also
- [../../backends/api/backend.yml](../../backends/api/backend.yml) — classifier metadata
- [../../backends/api/recipes/](../../backends/api/recipes/) — the recipe registry (drop-in)
- [../fortigate/README.md](../fortigate/README.md) — the device class that wraps this
- [../../../docs/capability-matrix.md](../../../docs/capability-matrix.md) — the design (§5.2 the API tail)
