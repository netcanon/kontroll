#!/usr/bin/env python3
"""kontroll-init — the from-scratch bootstrap a RUNNING GUI cannot do for itself (the keygen split's CLI half).

Run LOCALLY on a fresh control node, as the trusted operator, BEFORE the stack is up. It does the three steps
that are chicken-and-egg for the GUI (it needs them to run) or that were manual in SETUP.md §5:
  1. ensures the **control age key** (`age-keygen -o ~/.config/sops/age/keys.txt`, 0600) — the day-to-day
     decryptor the GUI/Semaphore mount read-only;
  2. adds its PUBLIC half to /.sops.yaml (base + ops groups) via the shared, parse-verified
     keygen.add_recipient (additive + idempotent — closes the manual SETUP §5.1 step);
  3. mints the `dashboards` BOOTSTRAP secrets if absent — the onboard-GUI password (rendered to GUI_PASSWORD;
     the genuine chicken-and-egg — the GUI can't set the password it needs to start), the Grafana admin
     password, and the C10 API token — encrypted into the dashboards SOPS domain (the recipient from step 2).

Unlike the GUI keygen (show-once, never persisted), this WRITES the control private key to its canonical 0600
path: the operator IS the trusted actor here and a running GUI mounts that file read-only, so only a local CLI
can create it. The minted GUI/Grafana passwords are shown ONCE for the operator to record. Idempotent: a second
run on a configured node makes no change. Reuses keygen.add_recipient + the secrets service (both tested).
See docs/SETUP.md §5 + docs/install-from-scratch.md §2 (the keygen split) + SECURITY.md C11.

A BRAND-NEW node instead runs `--fresh`: it scaffolds a private `instance/` overlay from the shipped
`instance.example/` skeleton, writes a `.sops.yaml` naming ONLY the freshly-minted control key (never inheriting
another instance's recipients — the default path is additive, which a fresh Joe must NOT do), and fills
`instance.yml`'s mgmt_ip/domain from flags. See docs/install-from-scratch.md (the genericization overlay).

Usage:  python3 scripts/kontroll-init.py                                   # existing overlay: add your key (additive)
        python3 scripts/kontroll-init.py --fresh --mgmt-ip <ip> --domain <d> --trust-mode separated   # brand-new node
"""
import argparse
import os
import re
import secrets as _sysrandom
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # so `from kontroll import …` resolves
from kontroll import catalog, paths  # noqa: E402
from kontroll.service import keygen  # noqa: E402
from kontroll.service import secrets as secrets_service  # noqa: E402

AGE_KEY_FILE = os.path.join(os.path.expanduser("~"), ".config", "sops", "age", "keys.txt")
CONTROL_ANCHORS = ["base_recipients", "ops_recipients"]   # the control key must decrypt EVERY domain
BOOTSTRAP_DOMAINS = ["dashboards", "semaphore"]            # auto-mintable bootstrap domains deploy-stack's .env needs
#   dashboards: GUI/Grafana passwords + the C10 API token; semaphore: DB pw + admin login + access-key key.
#   Device-cred domains (proxmox, network, …) stay operator-provided (real creds) — minted via onboarding.


def _parse_age_public(text):
    """The age PUBLIC recipient (age1…) from an age keys.txt — the `# public key:` comment, else the first
    age1 token. Returns None if absent. Pure (testable)."""
    m = re.search(r"#\s*public key:\s*(age1[0-9a-z]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"\bage1[0-9a-z]{20,}\b", text)
    return m.group(0) if m else None


def bootstrap_secret_values(form, existing):
    """Values kontroll-init must SUPPLY for a bootstrap domain: for each required field that is absent AND has
    no `generate:` spec, its declared `default` if it has one (e.g. the Semaphore admin username/email — config,
    not a secret to randomise), else a freshly generated login password. The service mints the `generate:`
    fields (e.g. the API token); an already-set field is kept. Returns {key: value}. Pure but for the CSPRNG."""
    out = {}
    for f in form.get("fields", []):
        if f["key"] in existing or f.get("generate") or not f.get("required"):
            continue
        out[f["key"]] = f["default"] if f.get("default") is not None else _sysrandom.token_urlsafe(18)
    return out


def ensure_control_key(key_file):
    """Ensure the control age key exists at key_file (age-keygen -o if absent; dir 0700, file 0600). Returns
    (public, created). Never prints the private key."""
    created = False
    if not os.path.exists(key_file):
        os.makedirs(os.path.dirname(key_file), mode=0o700, exist_ok=True)
        subprocess.run(["age-keygen", "-o", key_file], check=True, capture_output=True)
        os.chmod(key_file, 0o600)
        created = True
    with open(key_file, encoding="utf-8") as fh:
        return _parse_age_public(fh.read()), created


def scaffold_overlay():
    """Copy the shipped instance.example/ skeleton into instance/ for a BRAND-NEW node (`--fresh`). NEVER
    overwrites an existing instance/ file (idempotent / safe to re-run) and NEVER copies the secrets/ subtree —
    a fresh node mints its OWN secrets, encrypted to its OWN key; it must not inherit ciphertext. The top-level
    README is skeleton docs, not overlay config, so it is skipped. Returns (created, kept) lists of overlay-
    relative paths, or None if the tool shipped without the skeleton. Reads paths.ROOT dynamically (tmp-repo
    testable)."""
    src = os.path.join(paths.ROOT, "instance.example")
    if not os.path.isdir(src):
        return None
    dst = os.path.join(paths.ROOT, paths.INSTANCE_DIR_NAME)
    created, kept = [], []
    for root, _dirs, files in os.walk(src):
        rel_root = os.path.relpath(root, src)
        parts = [] if rel_root == "." else rel_root.split(os.sep)
        if parts and parts[0] == "secrets":
            continue                                       # never scaffold secret ciphertext (there is none)
        for fn in files:
            if rel_root == "." and fn == "README.md":
                continue                                   # skeleton docs, not overlay config
            if rel_root == "." and fn == "leak-tokens.example.txt":
                fn_out = "leak-tokens.txt"                 # the identifier-token list lands under its LIVE name
            else:                                          # (tests/_leak_guard.py reads instance/leak-tokens.txt)
                fn_out = fn
            rel = fn_out if rel_root == "." else os.path.join(rel_root, fn_out)
            target = os.path.join(dst, rel)
            relposix = rel.replace(os.sep, "/")
            if os.path.exists(target):
                kept.append(relposix)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(os.path.join(root, fn), target)
            created.append(relposix)
    return created, kept


def _personalize_instance_yml(mgmt_ip, domain, tls_mode, trust_mode=None):
    """Set the scaffolded instance/instance.yml's mgmt_ip / domain / frontend.tls_mode / trust_mode from the
    supplied flags (each optional — an omitted value leaves the template as-is). Value-only, line-oriented
    substitution so the file's comments survive (no YAML round-trip). `trust_mode` (F3) ships COMMENTED in the
    template (`# trust_mode: separated`), so the match tolerates an optional `# ` prefix and writes the ACTIVE
    form — uncommenting the operator's chosen posture while leaving an unchosen key commented (the never-defaulted
    contract). Returns the keys set."""
    path = paths.resolve("config/instance.yml", write=True)   # -> instance/instance.yml (overlay present post-scaffold); a WRITER, never the example
    if not os.path.exists(path):
        return []
    subs = {"mgmt_ip": mgmt_ip, "domain": domain, "tls_mode": tls_mode, "trust_mode": trust_mode}
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    set_keys = []
    for i, ln in enumerate(lines):
        for key, val in subs.items():
            if val is None:
                continue
            # `(#\s*)?` tolerates a COMMENTED template line (trust_mode ships commented); the write drops the
            # comment marker + keeps the indent, so a commented key is uncommented-and-set, an active key is replaced.
            pat = re.compile(r"^(\s*)(#\s*)?(%s:\s*)\S+" % re.escape(key))
            if pat.match(ln):
                lines[i] = pat.sub(lambda m, v=val: m.group(1) + m.group(3) + v, ln, count=1)
                if key not in set_keys:
                    set_keys.append(key)
    if set_keys:
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
    return set_keys


def _personalize_homepage(mgmt_ip, domain):
    """Fill the scaffolded homepage services.yaml's __MGMT_IP__ / __DOMAIN__ sentinels from the --fresh flags, so a
    brand-new node's :3000 shows the real control-plane tiles on first boot (closes the #72 stub leak where a fresh
    portal pointed every tile at RFC-5737 TEST-NET / example.com). Literal token replacement — the file is operator-
    editable YAML (not a key-value config), so a string `.replace` preserves its comments + structure with no YAML
    round-trip. Each flag optional: an omitted value leaves its sentinel for the operator to edit by hand. Returns
    the tokens replaced (empty if the file is absent — a tool shipped without the homepage skeleton, or a partial
    overlay). Idempotent: a re-run after the operator edited the file finds no sentinels and changes nothing. The
    homepage file is NOT in paths._OVERLAY_MAP, so resolve it directly under the (dynamically read) overlay root."""
    hp = os.path.join(paths.ROOT, paths.INSTANCE_DIR_NAME, "dashboards", "homepage", "services.yaml")
    if not os.path.exists(hp):
        return []
    with open(hp, encoding="utf-8") as fh:
        text = fh.read()
    done = []
    for token, val in (("__MGMT_IP__", mgmt_ip), ("__DOMAIN__", domain)):
        if val is not None and token in text:
            text = text.replace(token, val)
            done.append(token)
    if done:
        with open(hp, "w", encoding="utf-8") as fh:
            fh.write(text)
    return done


def _fresh_guard():
    """Refuse `--fresh` on an ALREADY-CONFIGURED node — instance/.sops.yaml exists, names a real recipient, and
    carries no placeholder. Protects an existing overlay (the source instance, or a re-run after setup) from
    being treated as brand-new. Returns an error string to print, or None to proceed."""
    sops = paths.resolve(".sops.yaml")
    if not os.path.exists(sops):
        return None
    with open(sops, encoding="utf-8") as fh:
        txt = fh.read()
    if keygen.FRESH_PLACEHOLDER not in txt and re.search(r"\bage1[0-9a-z]{20,}\b", txt):
        return ("ERROR: %s already names real recipients — this node is configured.\n"
                "       --fresh is for a BRAND-NEW node. To add your key to the existing overlay, run "
                "kontroll-init WITHOUT --fresh." % os.path.relpath(sops, paths.ROOT))
    return None


def main(argv):
    ap = argparse.ArgumentParser(description="Bootstrap a fresh kontroll control node (keygen split, CLI half).")
    ap.add_argument("--key-file", default=AGE_KEY_FILE, help="control age key path (default: %(default)s)")
    ap.add_argument("--fresh", action="store_true",
                    help="scaffold a BRAND-NEW instance overlay from instance.example/ with YOUR key only "
                         "(never inherits another instance's recipients)")
    ap.add_argument("--mgmt-ip", dest="mgmt_ip", help="(with --fresh) this node's management IP, written to instance.yml")
    ap.add_argument("--domain", help="(with --fresh) this instance's base domain, written to instance.yml")
    ap.add_argument("--tls-mode", dest="tls_mode", choices=["byo_proxy", "self_signed", "acme"],
                    help="(with --fresh) frontend.tls_mode in instance.yml")
    ap.add_argument("--trust-mode", dest="trust_mode", choices=["solo", "separated"],
                    help="(with --fresh, REQUIRED) how this instance promotes staged proposals: 'separated' (a "
                         "human runs the promote-proposal Semaphore task — prod) or 'solo' (declares intent to "
                         "auto-promote; this edition has no auto-promoter, so it behaves like 'separated'). NEVER "
                         "defaulted — choose. See docs/trust-mode.md.")
    args = ap.parse_args(argv)

    # 0. --fresh: scaffold a BRAND-NEW overlay (YOUR key only) before the keygen steps -------------------
    if args.fresh:
        # F3 — the "never a silent default" rule enforced at BIRTH (the only place a fresh overlay is created):
        # --fresh MUST name a trust posture. kontroll fail-LOUDS here (deploy-stack asserts again, belt-and-braces)
        # rather than guess whether you want human-reviewed (separated) or auto (solo) promotion. (tls_mode fail-SAFEs
        # to self_signed; trust posture is the opposite — guessing it is never safe.)
        if not args.trust_mode:
            print("ERROR: --fresh requires --trust-mode {separated|solo} — kontroll never silently picks your trust "
                  "posture (how staged proposals are promoted). 'separated' = a human reviews+promotes each (prod); "
                  "'solo' = declares intent to auto-promote (this edition has no auto-promoter, so it behaves like "
                  "'separated'). See docs/trust-mode.md.",
                  file=sys.stderr)
            return 1
        guard = _fresh_guard()
        if guard:
            print(guard, file=sys.stderr)
            return 1
        sc = scaffold_overlay()
        if sc is None:
            print("ERROR: --fresh needs the shipped instance.example/ skeleton, which is absent.", file=sys.stderr)
            return 1
        created_files, kept = sc
        print("==> instance overlay scaffolded from instance.example/ (%d new, %d kept)"
              % (len(created_files), len(kept)))
        set_keys = _personalize_instance_yml(args.mgmt_ip, args.domain, args.tls_mode, args.trust_mode)
        if set_keys:
            print("    instance.yml set: %s" % ", ".join(set_keys))
        else:
            print("    edit instance/instance.yml: set mgmt_ip + domain (left as placeholders).")
        hp_done = _personalize_homepage(args.mgmt_ip, args.domain)
        if hp_done:
            print("    homepage tiles personalized: %s" % ", ".join(hp_done))
        else:
            print("    edit instance/dashboards/homepage/services.yaml: replace __MGMT_IP__/__DOMAIN__ sentinels.")

    # 1. control age key ---------------------------------------------------------------------------------
    public, created = ensure_control_key(args.key_file)
    if not public:
        print("ERROR: could not read the age public key from %s" % args.key_file, file=sys.stderr)
        return 1
    print("==> control age key: %s  (%s)" % (public, "GENERATED" if created else "already present"))
    print("    private: %s (0600) — keep a copy in your password manager; it decrypts every secret." % args.key_file)

    # 2. .sops.yaml recipient — SEED (fresh: your key only, replace) vs ADD (existing overlay: additive) ----
    if args.fresh:
        w = keygen.seed_fresh_sops(public)               # REPLACE the placeholder — never inherit other recipients
    else:
        w = keygen.add_recipient(public, CONTROL_ANCHORS)   # additive, parse-verified, idempotent
    if w["error"]:
        print("ERROR: could not write the control recipient to .sops.yaml: %s" % w["error"], file=sys.stderr)
        return 1
    if args.fresh:
        status = "SEEDED (your key only)" if w["changed"] else "already seeded"
    else:
        status = "ADDED" if w["changed"] else "already present"
    print("==> .sops.yaml recipient: %s (domains: %s)" % (status, ", ".join(w["recipient_domains"]) or "(none)"))
    if w["changed"] and not args.fresh:
        print("    NEXT: re-wrap any pre-existing secrets to this recipient: %s" % keygen.SOPS_UPDATEKEYS)

    # 3. bootstrap secrets for the auto-mintable domains — deploy-stack's docker/.env render REQUIRES the
    #    dashboards (GUI/Grafana pw + C10 token) AND semaphore (DB pw + admin login + access-key) domains BEFORE
    #    the GUI (where the remaining, operator-provided secrets are entered) can come up. ------------------
    for domain in BOOTSTRAP_DOMAINS:
        form = catalog.secret_form(domain)
        if form is None:
            print("WARNING: no '%s' secret-form descriptor; skipping its bootstrap secrets." % domain)
            continue
        existing = secrets_service._existing_keys(domain)
        field_keys = {f["key"] for f in form.get("fields", [])}
        if field_keys and field_keys <= existing:
            # Fully provisioned already — skip (fast no-op + a clear log line). Since #141 build_secret_plan no
            # longer re-mints an already-set `generate:` field on a blank/no-`regenerate` call (it KEEPS it), so a
            # re-run is idempotent either way; this stays as the explicit "nothing to do" path. (Partial domains
            # still fall through below to mint only the ABSENT fields.)
            print("==> %s bootstrap secrets: already set (no change)" % domain)
            continue
        supplied = bootstrap_secret_values(form, existing)
        plan = secrets_service.build_secret_plan(domain, supplied)
        if plan["error"]:
            print("ERROR: %s plan: %s" % (domain, plan["error"]), file=sys.stderr)
            return 1
        applied = secrets_service.apply_secret_plan(plan)
        if applied["error"]:
            print("ERROR: could not write the %s domain: %s (is the age key present to encrypt?)"
                  % (domain, applied["error"]), file=sys.stderr)
            return 1
        if applied["changed"]:
            print("==> %s bootstrap secrets written (encrypted to the control key). SAVE these now:" % domain)
            for v in plan["view"]:
                if v["source"] in ("provided", "generated"):
                    shown = supplied.get(v["key"])
                    print("    %-28s %s" % (v["key"] + ":", shown if shown else "(generated, stored — used by the deploy)"))
        else:
            print("==> %s bootstrap secrets: already set (no change)" % domain)

    print("\n==> kontroll-init done. Next:")
    print("    - SAVE the GUI/Grafana passwords above (your onboard-GUI + Grafana logins).")
    print("    - Break-glass + scoped Semaphore keys: the GUI 'Keys' dialog (after deploy), or SETUP.md §5.2.")
    print("    - Enter the remaining service secrets: the GUI 'Secrets' dialog, or `sops instance/secrets/<d>.sops.yml`.")
    print("    - Next: edit instance/fleet.yml and instance/inventory/hosts.yml, then the dry-run —")
    print("      `scripts/kontroll-installer.sh check` (`./kontroll check` from a launch kit).")
    print("      The whole sequence is the quickstart at the top of docs/install-from-scratch.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
