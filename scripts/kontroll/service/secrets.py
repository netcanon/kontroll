"""secret-onboarding domain — guided entry of a SOPS domain's service/infra secrets (the secret analogue of
onboard.py). A PURE plan builder (reads the descriptor + the domain's EXISTING key NAMES — never values) +
an apply step that merges the new values into the domain and re-encrypts it via gitio.sops_write_domain,
returning the path for the route to commit + STAGE (proposed/<run_id>, C10).

Secret values flow through `values` IN MEMORY only — they are NEVER logged, returned, or committed in
plaintext. The plan VIEW exposes field NAMES + which are already set + which will be generated, never a value.
Recipiency stays owned by /.sops.yaml; this module only writes the domain's encrypted contents.
"""
import base64
import logging
import os
import re
import secrets as _sysrandom

import yaml

from kontroll import catalog, gitio, paths

log = logging.getLogger("kontroll.service.secrets")

# generate: <spec> in a descriptor field -> a CSPRNG minter run on the box (value never returned/logged).
_GENERATORS = {
    "hex32": lambda: _sysrandom.token_hex(32),                       # openssl rand -hex 32
    "base64_32": lambda: base64.b64encode(_sysrandom.token_bytes(32)).decode(),  # openssl rand -base64 32
}

# actuation.kind closed enum (validated over the descriptors by test_secrets_service.py) + the safe charsets for
# the values interpolated into an operator-runnable enact STRING — defense-in-depth so a poisoned/typo'd
# secret-forms descriptor can never emit a shell-metachar command (makes the closed-enum-and-validate promise real).
_ACTUATION_KINDS = frozenset({"recreate", "grafana-cli", "semaphore-admin", "none"})
_SAFE_SERVICE = re.compile(r"\A[A-Za-z0-9._-]+\Z")     # a compose service name (no shell metachars; \Z = no trailing-\n slip)
_SAFE_EXEC = re.compile(r"\A[A-Za-z0-9 ._/-]+\Z")      # a grafana-cli subcommand (spaces ok; no metachars; airtight end-anchor)

# shape: <name> in a descriptor field -> the form a PROVIDED value must take, checked before anything is encrypted (a
# closed set, validated over the descriptors by test_secrets_service.py; an unknown name refuses every value, and
# every shape refuses a value that is not a string before its rule is asked).
# http_url: one http(s) URL that docker/.env carries in single quotes and Vector can redact (T32: the notifier URL). A
# `'` ends the quote (and a line compose cannot parse, it prints in the error of an un-no_logged task); a space, a `"`,
# a `\` or a character Go's %q escapes stops the redaction's path in Grafana's failed-delivery line; a `$` or a `#`
# would change the line were it unquoted. The rule is "printable, and none of these six", so deploy-stack.yml can state
# it without a pattern (it must hold a value set with sops by hand, and a Jinja pattern's backslashes do not survive
# both ansible cores alike).
_HTTP_URL = re.compile(r"\Ahttps?://[^ \"'$#\\]+\Z")
_SHAPES = {
    "http_url": (lambda v: isinstance(v, str) and v.isprintable() and bool(_HTTP_URL.match(v)),
                 "one http(s) URL with no whitespace or other non-printable character, quote, backslash, $ or #"),
}


def _existing_keys(domain):
    """Top-level NON-`sops` keys already present in instance/secrets/<domain>.sops.yml (NAMES only — the file
    is encrypted, the values are ciphertext; we never decrypt here). Empty set if the domain file is absent."""
    path = paths.resolve("ansible/secrets/%s.sops.yml" % paths.component(domain, "secret domain"))
    if not os.path.exists(path):
        return set()
    try:
        doc = yaml.safe_load(open(path, encoding="utf-8")) or {}
    except yaml.YAMLError:
        return set()
    return {k for k in doc if k != "sops"}


def offerable_fields(domain):
    """The descriptor's fields annotated with `already_set` (is the key present in the domain file?), so the
    form can show set/unset WITHOUT revealing any value (only key NAMES are read). Returns
    {"error": "no_form"|None, "domain","label","description","recipients","fields":[...]}."""
    form = catalog.secret_form(domain)
    if form is None:
        return {"error": "no_form"}
    existing = _existing_keys(domain)
    fields = [{"key": f["key"], "label": f.get("label", f["key"]), "type": f.get("type", "text"),
               "required": bool(f.get("required")), "generate": f.get("generate"),
               "default": f.get("default"), "help": f.get("help", ""),
               "rotatable": bool(f.get("rotatable")),   # opt-in: this field may be rotated (overwrite + actuate)
               "already_set": f["key"] in existing} for f in form.get("fields", [])]
    return {"error": None, "domain": domain, "label": form.get("label", domain),
            "description": form.get("description", ""), "recipients": form.get("recipients", "base"),
            "fields": fields}


def build_secret_plan(domain, values, regenerate=None):
    """PURE: resolve which fields to write for `domain` from the submitted `values` ({key: value}, in memory).
    Each descriptor field becomes provided | generated | already_set | skipped. A required field with no value,
    no generator, and not already set is an error. Returns {"error": "no_form"|"missing:<key>"|None, "domain",
    "to_set": {key: VALUE}, "view": [{key,label,source}], "path"} — `to_set` holds the SECRET values (apply
    consumes it; NEVER returned to a client); `view` is the client-safe summary (NAMES + source, no values).
    A provided value its field's `shape:` refuses is {"error": "bad_value:<key>", "hint": <the shape's words>}
    (the key and the rule, never the value).

    `regenerate` is the set of keys the caller EXPLICITLY asked to re-mint (the GUI 'gen' button — #141). A
    `generate:` field is minted ONLY when it is provided-blank AND (explicitly in `regenerate` OR ABSENT, i.e. a
    first-time mint). A blank `generate:` field that is ALREADY SET and NOT in `regenerate` is KEPT — so rotating
    one field never SILENTLY re-mints another already-set generated secret (the C-7/SEC-2 footgun). Onboarding a
    fresh node still mints absent generated fields; kontroll-init (which passes no `regenerate`) keeps existing
    ones."""
    form = catalog.secret_form(domain)
    if form is None:
        return {"error": "no_form"}
    existing = _existing_keys(domain)
    regen = set(regenerate or ())
    to_set, view = {}, []
    for f in form.get("fields", []):
        key, gen, req = f["key"], f.get("generate"), bool(f.get("required"))
        provided = (values or {}).get(key)
        if provided and f.get("shape") is not None:
            takes, hint = _SHAPES.get(f["shape"], (lambda v: False, "a value of the unknown shape %r" % f["shape"]))
            if not isinstance(provided, str) or not takes(provided):   # a shape takes a string only, whatever its rule
                return {"error": "bad_value:%s" % key, "hint": hint}
        if provided:
            to_set[key] = provided
            source = "provided"
        elif gen and gen in _GENERATORS and (key in regen or key not in existing):
            to_set[key] = _GENERATORS[gen]()        # minted on the box (explicit regen OR first-time); never returned
            source = "generated"
        elif key in existing:
            source = "already_set"                   # blank + already set + not regen-requested -> KEEP (no re-mint)
        elif req:
            return {"error": "missing:%s" % key}
        else:
            source = "skipped"
        view.append({"key": key, "label": f.get("label", key), "source": source})
    return {"error": None, "domain": domain, "to_set": to_set, "view": view,
            "path": paths.overlay_target("ansible/secrets/%s.sops.yml" % paths.component(domain, "secret domain"))}


def apply_secret_plan(plan):
    """Merge the plan's `to_set` (in-memory secret values) into the domain and re-encrypt it: decrypt the
    existing domain (or {} if new), update the new keys, write it back via gitio.sops_write_domain (whole-file,
    stdin — no argv/temp-file leak). Idempotent: identical plaintext re-encrypts to nothing (changed=False).
    Returns {"changed": bool, "paths": [domain path], "error": None|"decrypt_failed"|"encrypt_failed"}.
    Prints NO value (gitio only prints counts)."""
    domain, to_set = plan["domain"], plan["to_set"]
    if not to_set:
        return {"changed": False, "paths": [plan["path"]], "error": None}
    current = gitio.sops_decrypt_domain(domain)
    if current is None:
        return {"changed": False, "paths": [plan["path"]], "error": "decrypt_failed"}
    merged = dict(current)
    merged.update(to_set)
    if merged == current:                            # nothing actually changes -> no re-encrypt, no git churn
        return {"changed": False, "paths": [plan["path"]], "error": None}
    if not gitio.sops_write_domain(domain, merged):
        return {"changed": False, "paths": [plan["path"]], "error": "encrypt_failed"}
    return {"changed": True, "paths": [plan["path"]], "error": None}


def overwrite_set(domain, plan):
    """The NAMES of fields this plan would OVERWRITE — already present in the domain ∩ being written
    (provided|generated). NAMES only — never a value, never a before→after (C11). Re-derived SERVER-SIDE from
    the domain file (`_existing_keys`), never trusting a client-asserted set (M1). The route/GUI fail CLOSED on a
    non-empty result unless the caller acked `overwrite` — the C9 measure-twice-cut-once gate for clobbering a
    LIVE secret: rotating an already-set value is an overwrite; onboarding a fresh field is not."""
    existing = _existing_keys(domain)
    written = {v["key"] for v in plan.get("view", []) if v.get("source") in ("provided", "generated")}
    return sorted(written & existing)


def actuation_enact_commands(domain, set_names):
    """The post-promote steps to make the just-staged password(s) LIVE on the running service — the GUI/API
    EXECUTE none of them (mirrors observe.telemetry_enact_commands; a network surface never actuates and holds
    no docker socket). For each ROTATABLE field actually being set, read its descriptor `actuation:` block and
    emit a {field, kind, cmd, why, consequence} record. The secret VALUE never appears — the running service
    reads it from the re-rendered .env / from SOPS at exec time. A rotatable field with NO `actuation` block is
    STAGE-ONLY (the value is recorded; emits nothing). `actuation.kind` is a closed enum: recreate | grafana-cli
    | semaphore-admin | none — a new live-change style is the only thing that adds a branch here (a reviewed
    function), the same way a new `generate:` spec adds a `_GENERATORS` entry; a service reusing an existing
    style is pure YAML."""
    form = catalog.secret_form(domain) or {}
    by_key = {f["key"]: f for f in form.get("fields", [])}
    out = []
    for name in set_names:
        f = by_key.get(name) or {}
        if not f.get("rotatable"):
            continue
        act = f.get("actuation") or {}
        kind, svc = act.get("kind", "none"), act.get("service")
        if kind not in _ACTUATION_KINDS:
            continue   # unknown/typo'd kind -> stage-only (fail-safe); the descriptor schema test rejects it in CI
        if kind in ("recreate", "grafana-cli", "semaphore-admin") and not (svc and _SAFE_SERVICE.match(svc)):
            continue   # a service with shell metachars (or absent) never reaches an enact cmd (defense-in-depth)
        if kind == "recreate":
            cmd = ("cd ~/kontroll/ansible && ansible-playbook playbooks/deploy-stack.yml "
                   "-e '{\"stack_services\":[\"%s\"]}'" % svc)
            if act.get("api_privileged"):
                cmd += " -e api_privileged=true"
            out.append({"field": name, "kind": "operator", "cmd": cmd,
                        "why": "re-render docker/.env from the promoted SOPS value, then recreate %s "
                               "(it reads the password from its env at start)" % svc,
                        "consequence": act.get("consequence", "none")})
        elif kind == "grafana-cli":
            ex = act.get("exec", "")
            if not _SAFE_EXEC.match(ex):
                continue   # never interpolate a metachar-bearing exec into the operator-runnable cmd string
            out.append({"field": name, "kind": "operator",
                        "cmd": "docker exec -i %s %s   # feed the value on stdin, never on argv" % (svc, ex),
                        "why": "Grafana applies its admin env only at first DB init; reset the RUNNING admin "
                               "in-container (keep the staged value so a future re-init matches)",
                        "consequence": "in-container exec — run from the operator shell (the Semaphore runner "
                                       "has no docker socket, so this stays operator-run)"})
        elif kind == "semaphore-admin":
            # Semaphore applies SEMAPHORE_ADMIN_PASSWORD only at FIRST-RUN `semaphore setup` (config.json absent);
            # on an existing Postgres admin a recreate does NOT reset the password. So the live rotation is a
            # two-step hand-off: (1) re-render .env + recreate so the promoted value is consistent AND lands in the
            # container env; (2) `users change-by-login` upserts the RUNNING admin, reading login+password from the
            # container's OWN env — the value never touches the host argv or the shell history. (Verified live on
            # v2.18.12: server-wrapper sets the admin only inside the FIRST_RUN block; change-by-login is the
            # deterministic path. No `exec` is taken from the descriptor — the template is hardcoded; only the
            # validated `service` is interpolated.)
            recreate = ("cd ~/kontroll/ansible && ansible-playbook playbooks/deploy-stack.yml "
                        "-e '{\"stack_services\":[\"%s\"]}'" % svc)
            upsert = ("docker exec %s sh -c 'semaphore users change-by-login "
                      "--login \"$SEMAPHORE_ADMIN\" --password \"$SEMAPHORE_ADMIN_PASSWORD\" "
                      "--config /etc/semaphore/config.json'" % svc)
            out.append({"field": name, "kind": "operator", "cmd": recreate + "\n" + upsert,
                        "why": "Semaphore sets the admin only at first-run setup; the live login is rotated by an "
                               "in-container `users change-by-login` that reads the new value from the container's "
                               "own env (set by the recreate) — no value on host argv or in shell history",
                        "consequence": act.get("consequence",
                                               "recreates semaphore — drops in-flight jobs + this UI session")})
        # kind == "none" → stage-only; emit nothing
    return out
