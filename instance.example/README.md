# instance.example/ — the instance overlay TEMPLATE (shipped; generic)

`kontroll` is a **tool**; everything that makes a deployment *yours* — your secrets,
your age recipients, your inventory, your management IP and domain, your fleet
selection — lives in a private **`instance/` overlay** that the tool reads in
preference to its shipped defaults (resolver: `scripts/kontroll/paths.py`).

This `instance.example/` tree is the **placeholder skeleton** of that overlay. It
ships with the tool so a fresh operator has a working shape to fill in. It contains
**no real secrets and no real recipients** — only RFC 5737 / RFC 2606 documentation
placeholders (`192.0.2.x`, `example.com`) and one obviously-fake age recipient.

## Standing up your overlay

The guided path mints your control key and personalizes this skeleton in one step:

```bash
scripts/kontroll-installer.sh fresh-init --mgmt-ip <your-mgmt-ip> --domain <your-domain> --trust-mode separated \
    --break-glass-recipient <age1…>
```

(From a launch kit: `./kontroll fresh-init …`. `--trust-mode` is the promote posture —
[docs/trust-mode.md](../docs/trust-mode.md); what the scaffold does when it is left out depends on the edition,
so always name it. `<age1…>` is the public half of a break-glass key you make on another machine; the scaffold
refuses to run without a break-glass choice — [docs/break-glass.md](../docs/break-glass.md) has the three. The
whole install sequence is the quickstart at the top of
[docs/install-from-scratch.md](../docs/install-from-scratch.md).)

`fresh-init` runs `kontroll-init --fresh` in the installer container, which:

1. copies this skeleton to `instance/` (never overwriting an existing file, never
   copying secret ciphertext — there is none to copy);
2. mints your control age key (`~/.config/sops/age/keys.txt`, 0600);
3. writes `instance/.sops.yaml` naming **only your key** — it never inherits another
   instance's recipients (the placeholder below is *replaced*, not appended to);
4. adds the break-glass recipient you chose under both recipient anchors (or, with
   `--no-break-glass`, says that every domain has one recipient);
5. fills `instance/instance.yml`'s `mgmt_ip` / `domain` from the flags (or leave them
   and edit by hand);
6. mints the bootstrap login secrets (the `dashboards` and `semaphore` domains),
   encrypted to your key and to the break-glass key if you chose one.

Add the scoped-Semaphore key afterward via the GUI **Keys** dialog — and a break-glass key the same way, or with
[docs/break-glass.md](../docs/break-glass.md), if you declined one — then re-wrap:

```bash
sops --config instance/.sops.yaml updatekeys instance/secrets/*.sops.yml
```

## Layout

| Path | What it is |
|---|---|
| `.sops.yaml` | SOPS creation rules — which age key encrypts which domain. **Replace the placeholder recipient.** |
| `instance.yml` | source-of-truth mode, `mgmt_ip` / `domain`, backup remotes, frontend `tls_mode`. |
| `fleet.yml` | which device-class modules this instance runs (`enabled_modules`). |
| `inventory/hosts.yml` | your hosts, in FUNCTIONAL groups (`edge_firewall`, `core_switch`, …). |
| `secrets/` | your encrypted `*.sops.yml` — **created on your node, never shipped** (see its README). |
| `dashboards/homepage/` | the Homepage portal tiles/settings — edit for your services. |
| `actuation/<key>/` | the app-store units you create in the GUI (`unit.yml`) and their values (`vars.yml`). Not in this skeleton: the first unit creates it. |
| `certs/` | PUBLIC material you pin in `instance.yml` (`device_trust.<key>.tls_ca_file`, `ssh_known_hosts_file`): a CA certificate, a `known_hosts` file. Commit them — the deploy reads them from this tree. Never a private key. Not in this skeleton. |
| `OPERATIONS.md` | this deployment's own ledger: its boxes and their roles, what must not be touched, standing rules, access paths (never a secret — where it lives), key custody, owed lab actions, a live-run log. The tool's state is the repository's `STATUS.md`; this is yours. `tests/check-status.py` warns while its as-of date is unfilled. |
| `.gitignore` | re-includes the overlay's known entries under the root `/instance/*` rule, so your instance repository — and the control node's canonical — tracks them. Add a line when you add a new top-level entry. An instance scaffolded before `actuation/` and `certs/` were on the list needs those two lines added by hand (`!/actuation/`, `!/certs/`): the scaffold never overwrites a file you have. After adding them, look at what `git status` now shows under `certs/` before you commit it, and see the CHANGELOG entry "a staged write lands whole, or not at all" for a unit an earlier attempt left in a service's clone. |
| `leak-tokens.example.txt` → `leak-tokens.txt` | the names that identify YOUR deployment (hostnames, domain, user names), one regex per line; `tests/_leak_guard.py` fails the gate if any appears in a file that ships publicly. Ships with synthetic canaries; `--fresh` copies it under its live name for you to fill in. |

A checkout with **no `instance/` at all** (a fresh clone, CI) reads this skeleton through the same
resolver (`kontroll.paths.resolve`, read-only — writers never target it), so the generators'
`--check` gates and the test suite run against these TEST-NET placeholders.

See `docs/SETUP.md`, `docs/install-from-scratch.md`, and `SECURITY.md` (C11).
