# instance/secrets/ — SOPS-encrypted secret domains (NEVER shipped)

This directory holds your encrypted `*.sops.yml` secret files — one per domain
(`network`, `proxmox`, `dashboards`, `semaphore`, …). **The tool ships none of them**,
and `kontroll-init --fresh` copies none into your overlay. They are created on YOUR
node, encrypted to YOUR key (the recipient in `instance/.sops.yaml`), by either:

- **`kontroll-init --fresh`** — mints the bootstrap `dashboards` secrets (the onboard-GUI
  + Grafana login passwords and the API token). Homepage widget tokens go in the same
  domain as `homepage_var_<name>` keys — deploy-stack renders ONLY those into Homepage's
  own env file (`docker/.env.homepage`); the portal never reads the stack `.env`; or
- the **onboard-GUI "Secrets" dialog** — guided, no-leak entry per domain; or
- `sops instance/secrets/<domain>.sops.yml` directly (type values into the editor — never
  on the command line; SECURITY.md C11).

After you add a recipient (break-glass or the scoped Semaphore key), re-wrap so the
existing files gain it:

```bash
sops --config instance/.sops.yaml updatekeys instance/secrets/*.sops.yml
```

Never commit a plaintext secret or an age private key. See `SECURITY.md` (C8 / C11).

**Where an onboard stores a device's credentials.** In the class's own `secrets_domain` when the onboard is a
second device of a class (whatever domain it names), else in the domain the onboard names, else in the backend's
`default_secrets_domain` (`ansible/backends/<name>/backend.yml` — `network` for every backend the tool ships). A domain you name that has no rule in `.sops.yaml`
falls to the catch-all, which the scoped Semaphore key is not on: add a rule for it before a scheduled job reads it.

**This directory holds sops documents and nothing else.** The deploy commits your working tree into the local
canonical, and refuses to do so — naming the file — while a `*.sops.yml` here has any value that is not ciphertext
(a file typed in an editor and never encrypted, one key added by hand to an encrypted file), or while any other
file sits here (a `sops -d … > network.yml` copy, an editor's `.bak`). A file that was never encrypted:
`sops --config instance/.sops.yaml -e -i instance/secrets/<domain>.sops.yml` (on a launch-kit host, inside
`./kontroll shell` — the host has no sops). Anything else: move it out of the repository. The refusal names a
rule, and `docs/install-from-scratch.md` ("REFUSING to commit") has the fix for each.
`scripts/lib/plaintext-guard.sh` is the check; `SECURITY.md` C1.
