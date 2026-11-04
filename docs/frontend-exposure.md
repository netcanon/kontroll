# Frontend exposure — how kontroll serves its web UIs

kontroll is **distributable**: a random operator stands it up with **zero certs/domain of their own**. How
the web frontends (the API now; the GUI/dashboards as they deploy) are served is a **per-instance mode**,
not a product assumption — `instance/instance.yml` `frontend.tls_mode`, resolved by the one dispatch seam
[`scripts/kontroll/frontend.py`](../scripts/kontroll/frontend.py) (covering check
[`tests/unit/test_frontend.py`](../tests/unit/test_frontend.py)). Re-scope rationale + history:
[docs/reviews/2026-06-13-roadmap/03-redesign-frontend-exposure.md](reviews/2026-06-13-roadmap/03-redesign-frontend-exposure.md).

## The three modes

| `frontend.tls_mode` | kontroll serves | Who owns certs/hostname | For |
|---|---|---|---|
| **`byo_proxy`** | services on **HTTP** (uvicorn `--forwarded-allow-ips=*`) | the operator's reverse proxy (NPM / Traefik / Caddy) | anyone with existing infra |
| **`self_signed`** (default) | services on **HTTPS** from a host-provisioned self-signed cert | nobody external (trust-on-first-use) | offline / no domain — the zero-config floor |
| **`acme`** | services on HTTP behind a bundled **Caddy** ingress doing **Let's Encrypt** | kontroll (operator brings a domain + DNS token) | operator with a domain, no proxy |

An unknown/empty mode resolves to **`self_signed`** (fail-safe — the API is never silently dropped to
plaintext).

## `byo_proxy` (this instance: Nginx Proxy Manager)
Services serve plain HTTP; the operator points their reverse proxy at them and owns TLS + hostnames + any
SSO. kontroll generates no cert and needs no domain. `--forwarded-allow-ips=*` makes uvicorn trust the
proxy's `X-Forwarded-For`, so the API rate-limiter keys off the **real client IP**, not the proxy hop.
Caveat: with a remote proxy, auth creds traverse the mgmt VLAN in cleartext — accepted-risk on a
single-operator mgmt-only VLAN (SECURITY C3), or co-locate the proxy (loopback) / keep self-signed upstream.
Pass the onboard GUI's responses through unchanged: its page runs only the script its response's
`Content-Security-Policy` nonce names (SECURITY C19), so a proxy that adds a policy of its own (the browser enforces
both) or rewrites the page's HTML (the tag's nonce must match the header's) can leave the GUI blank, and one
that replaces or removes the header leaves it working with no script policy, so nothing shows the layer is gone. A
second policy that names no script or style directive (a `frame-ancestors` of its own) is harmless.

## `self_signed` (the zero-config default)
deploy-stack generates a per-service self-signed cert (the API's uvicorn `--ssl-*`). Working HTTPS with
**zero inputs**; the operator imports the cert or accepts the browser warning. IP-friendly (no hostnames
needed), which is why it stays per-service uvicorn rather than Caddy.

## `acme` — bundled Caddy + Let's Encrypt
Set in `instance/instance.yml`:
```yaml
frontend:
  tls_mode: acme
  domain: kontroll.example.com     # services are served at <sub>.<domain> (api., semaphore., home., grafana., prometheus.)
  acme_email: ops@example.com
  dns_provider: cloudflare         # your Caddy DNS-01 provider module name
```
deploy-stack renders [`docker/services/caddy.yaml`](../docker/services/caddy.yaml)'s **Caddyfile** from this
(via `frontend.py render_caddyfile`) and appends `caddy` to the bring-up. Caddy obtains + auto-renews real
certs and reverse-proxies each service (HTTP backends) by hostname.

**Because kontroll is never WAN-exposed** (C3), the easy HTTP-01 challenge (needs a public `:80` inbound) is
unavailable — Caddy uses **DNS-01**, which needs:
1. a **public domain** you control + its DNS managed by a supported provider;
2. the provider's **API token** — stored SOPS-encrypted in `instance/secrets/acme.sops.yml` (a service domain,
   control + break-glass recipients only, **not** the Semaphore key) and rendered into `docker/.env` as
   `KONTROLL_ACME_DNS_TOKEN` (never written into the committed Caddyfile);
3. a **Caddy image carrying that provider's `caddy-dns` module** — the stock `caddy:2` has none; build one
   with [xcaddy](https://github.com/caddyserver/xcaddy) and set `KONTROLL_CADDY_IMAGE` in `docker/.env`.

> **Status: scaffolded + hermetic-tested + compose-validated; NOT live-verified in this lab** (it has no
> public domain / DNS provider, and this instance runs `byo_proxy`). The resolver mapping + Caddyfile
> generation are unit-tested; `caddy.yaml` passes `compose config`. First live operator with a real domain
> verifies issuance end-to-end. Use LE **staging** first (`render_caddyfile(..., staging=True)`) to avoid
> burning the production rate limit while wiring the DNS plugin.

## Adding a service to the Caddy ingress
One entry in `CADDY_BACKENDS` (`scripts/kontroll/frontend.py`): `key: (subdomain, "host:port")`. It is only
fronted when that key is in `stack_services` (the "add an X" test). Services that serve their own TLS (e.g.
onboard-gui) are intentionally not behind Caddy.

## See also
- [api-architecture.md](api-architecture.md) §9 — the API's deploy posture
- [../SECURITY.md](../SECURITY.md) — C1 (secret domains, incl. `acme`), C3 (reachability), C9 (the API surface),
  C19 (the onboard GUI's script policy)
- [../instance/instance.yml](../instance/instance.yml) — the `frontend` block (this instance)
- [../instance/secrets/README.md](../instance/secrets/README.md) — the `acme` secret domain
- [reviews/2026-06-13-roadmap/03-redesign-frontend-exposure.md](reviews/2026-06-13-roadmap/03-redesign-frontend-exposure.md) — the re-scope design
