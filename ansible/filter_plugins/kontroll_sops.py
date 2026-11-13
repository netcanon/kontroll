"""Ansible filter: decrypt one instance/secrets/<domain>.sops.yml to its dict, or {} if absent/undecryptable.

The generic decrypt-BY-NAME that lets deploy-stack decrypt the UNION of secret_env-referenced domains WITHOUT a
per-domain `vars:` lookup or a hardcoded domain->dict map (it kills `_sops_domains`). The SOPS domain name IS the
file stem, so a NEW credentialed device/exporter needs zero spine edit: it appears in the generated secret-env
manifest, this filter decrypts its file by stem, the render loop composes its env var.

FAIL-SOFT by contract: an un-onboarded domain (the file absent on a fresh node) yields `{}` so its env vars render
EMPTY, never a hard fail (preserves the "a fresh node without proxmox onboarded still deploys" posture). A decrypt
error likewise yields `{}` — never raises. It is applied PER domain (`map('kontroll_sops_domain', secrets_dir)`),
so one absent file never aborts the others (the per-domain fail-soft the union-decrypt requires).

Decrypts via the `sops` binary (the control node's deploy prereq) — version-independent, the same age key +
file-embedded recipients the platform-core `community.sops` lookups use. Runs INSIDE the `no_log: true` `.env`
render task (the value is composed there and nowhere else); the filter returns the dict but NEVER logs/prints it,
so no decrypted value escapes the no_log boundary (C12). Pairs with the `kontroll_render_token` filter (which
str.format-composes the env value from this dict, also fail-soft).
"""
import os
import re
import subprocess

import yaml

# A secret domain is a name the Secrets dialog takes (scripts/kontroll/paths.py `_COMPONENT_RE`; this plugin runs
# without the tool's package on its path, so tests/unit/test_gen_secret_env.py holds this copy to it): no `/` and no
# `.`, so no spelling of one names another file.
_DOMAIN_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
# The platform-core domains (scripts/gen-secret-env.py PLATFORM_CORE_DOMAINS, held equal by the same test):
# deploy-stack decrypts them fail-closed for its own .env lines, never through this filter.
_PLATFORM_CORE = frozenset({"semaphore", "dashboards"})


def kontroll_sops_domain(domain, secrets_dir):
    """domain (the SOPS file stem) + the resolved secrets dir -> the decrypted dict, or {} (absent/undecryptable).
    Never raises, never logs the plaintext. A domain that is not a name the Secrets dialog takes (`./dashboards`,
    `x/../dashboards`, a path to another file), or that is a platform-core one, is {} too: gen-secret-env refuses
    both, and this second seat does not rest on the committed manifest alone (T32 fold 3 read B, S1; fold 4's read
    100, N1, N2)."""
    if not domain or not secrets_dir:
        return {}
    if not isinstance(domain, str) or not _DOMAIN_RE.fullmatch(domain) or domain in _PLATFORM_CORE:
        return {}                                   # no path spelling reaches another file, nor this one a core file
    path = os.path.join(secrets_dir, "%s.sops.yml" % domain)
    if not os.path.exists(path):
        return {}                                   # un-onboarded domain — fail-soft to empty, never a hard fail
    try:
        proc = subprocess.run(["sops", "--decrypt", path],
                              capture_output=True, text=True, timeout=60)
    except Exception:
        return {}                                   # sops missing / not runnable — fail-soft
    if proc.returncode != 0:
        return {}                                   # decrypt failed (no key, bad recipients) — fail-soft
    try:
        return yaml.safe_load(proc.stdout) or {}
    except Exception:
        return {}


class FilterModule(object):
    def filters(self):
        return {"kontroll_sops_domain": kontroll_sops_domain}
