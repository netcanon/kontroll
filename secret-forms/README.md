# secret-forms — secret-onboarding FORM descriptors (NOT secrets)

> **This directory holds form SCHEMAS, never secret values.** Each file declares which fields a SOPS
> domain holds so the GUI can render a guided entry form. **Never put a real secret here** — values live
> only SOPS-encrypted under `instance/secrets/<domain>.sops.yml`. (The dir is deliberately named
> `secret-forms/`, not `secrets/`, to keep that distinction unmissable.)

One drop-in `secret-forms/<domain>.yml` per SOPS domain that should have a guided GUI entry form — the
secret analogue of `capabilities/<cap>.yml`. Adding a domain to the wizard = drop a file here; the shared
secret dialog (one shell, domain is data) + `service/secrets.py` need **no** edit.

## Schema

```yaml
domain: dashboards            # == the instance/secrets/<domain>.sops.yml basename
label: Dashboards & API token # human title for the dialog
description: ...              # one line shown under the title
order: 30                    # sort order in the picker (low = first)
recipients: base             # informational: the /.sops.yaml key_group (base | ops). NOT enforced here.
fields:
  - key: grafana_admin_password   # the SOPS key the value is encrypted under
    label: Grafana admin password # field label
    type: password                # password | token | text
    required: true
    generate: hex32               # OPTIONAL — offer a generate button (hex32 | base64_32)
    default: admin                # OPTIONAL — a non-secret default (text only)
    rotatable: true               # OPTIONAL — opt this field into the Secrets-dialog ROTATION UX (#141)
    actuation:                    # OPTIONAL — how a NEW value goes LIVE (post-promote hand-off; the GUI runs none)
      kind: recreate              #   recreate | grafana-cli | semaphore-admin | none (absent ⇒ stage-only)
      service: onboard-gui        #   the compose service the enact recreates / execs into
      consequence: self-evict     #   OPTIONAL — a warning shown to the operator (recreate drops the GUI session)
      exec: grafana cli ... --password-from-stdin   # grafana-cli kind ONLY — the in-container command (G13 form)
      api_privileged: true        #   recreate kind ONLY — append `-e api_privileged=true` to the enact string
    help: One-line guidance.
```

`type` drives the input: `password`/`token` render masked; `token` may carry `generate` to mint a random
value on the box (`hex32` = `openssl rand -hex 32`; `base64_32` = `openssl rand -base64 32`). `text` is a
plain non-secret field (usernames, emails). **Recipiency is owned by your instance's `.sops.yaml`** (`instance/.sops.yaml`; the shipped
template is [`instance.example/.sops.yaml`](../instance.example/.sops.yaml)), not this file — `recipients:` is documentation only.

## Rotation (`rotatable:` + `actuation:`, #141)

A `rotatable: true` field can be **re-saved with a new value** through the same dialog (rotation = providing a
new value for an already-set key). Two guards apply, both as DATA:

- **Overwrite confirm (the C9 measure-twice gate).** Re-saving an already-set field would clobber a LIVE secret,
  so the server re-derives the overwrite NAMES (`secrets.overwrite_set`) and **fails closed** (HTTP 409 /
  `secret-overwrite-confirm`) until the caller acks `overwrite:true`. NAMES only — never a value (C11).
- **Actuation hand-off (NOT a GUI action).** Staging a new value does not make it live. The optional
  `actuation:` block tells `secrets.actuation_enact_commands` which **command STRINGS** to surface for the
  operator/Semaphore to run *after promote* — the GUI/API execute none of them (no docker socket). `kind` is a
  closed enum: `recreate` (re-render `.env` + `deploy-stack -e stack_services=[<service>]`), `grafana-cli`
  (`docker exec -i <service> <exec>` — Grafana honours its admin env only at first DB init), `semaphore-admin`
  (recreate **then** an env-ferried `semaphore users change-by-login` — Semaphore sets the admin only at
  first-run setup, so recreate-alone can't rotate an existing admin; the CLI reads login+password from the
  container's OWN env, never the host argv/shell history), or `none`/absent (**stage-only**: recorded, not
  actuated).
- **Generated fields re-mint only on an EXPLICIT signal (#141).** A blank `generate:` field is KEPT on save; it
  re-mints ONLY when the operator clicks its `gen` button (which sends the field in the POST `regenerate` list).
  So rotating one field never silently re-mints another already-set generated secret (e.g. the
  `kontroll_api_token`, self-invalidating the caller's own bearer token). A first-time (ABSENT) generated field
  is still minted automatically — the change affects already-set fields only.

**Per-FIELD allow-list — leave rotation TRAPS un-flagged.** `rotatable` is opt-in per field, not per domain:
`semaphore_db_password` and `semaphore_access_key_encryption` are deliberately NOT `rotatable` because rotating
either alone breaks the running service (a live Postgres role / the encrypted cred store). Adding a new
`actuation.kind` is the only thing that touches code (a reviewed enact builder); a service reusing an existing
kind is pure YAML here.

## What writes the values

The shared dialog collects values; `scripts/kontroll/service/secrets.py` merges them into the domain and
re-encrypts the **whole file** via `gitio.sops_write_domain` (plaintext over sops' STDIN + `--filename-override`
— no argv/temp-file leak; creates the domain file if missing), then the privileged route commits + **stages**
`proposed/<run_id>` (C10 propose-then-promote). Values are **never** returned, logged, or committed in
plaintext — only the SOPS ciphertext and the field NAMES.

## Not every domain belongs here

`network` / `proxmox` / `compute` hold **per-host device credentials** set by the **device-onboarding form**
(`secrets=<domain>`), so they are NOT wizarded here (a shared form would collide with the per-host keys).
This registry is for **service/infra** secrets (dashboards, semaphore, snmp, acme).

## See also
- [instance/.sops.yaml](../instance/.sops.yaml) — the authoritative recipiency rules
- [key-roles/README.md](../key-roles/README.md) — the sibling keygen registry (the age-key analogue)
- [instance/secrets/README.md](../instance/secrets/README.md) — the domains + what each holds
- [SECURITY.md](../SECURITY.md) — C1 (encryption-at-rest) + C11 (guided secret + key onboarding, no-leak) +
  **C9** (the rotation overwrite gate + the actuation hand-off — governs the `rotatable:`/`actuation:` knobs above)
