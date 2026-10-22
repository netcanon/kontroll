"""age key-generation domain — the GUI half of the keygen SPLIT (the CLI `kontroll-init` mints the FIRST
control key + GUI_PASSWORD, a bootstrap a running GUI can't do; this mints break-glass / scoped-Semaphore /
control-rotation keys, all of which a GUI that already holds the control key can do).

The security contract is the INVERSE of secrets.py: keygen MINTS a keypair, and the PRIVATE half is the crown
jewel. It is shown to the operator EXACTLY ONCE (the apply result's `private_key`, which the route returns in
that single response) and is NEVER persisted by the service — not to the box, not to the repo — nor logged,
audited, or committed. Only the PUBLIC recipient (`age1…`) is durable: it is added to /.sops.yaml ADDITIVELY,
idempotently, and PARSE-VERIFIED (no existing recipient may be dropped) before the file is written. Minting a
keypair + adding a public recipient are public-key operations needing NO age key, so this fits C10's no-key
posture; the decrypt-needing re-wrap (`sops updatekeys`) is a DEFERRED operator step, never auto-run. Adding a
key role = drop a key-roles/<role>.yml file; no edit here. See key-roles/README.md + SECURITY.md (C11).
"""
import logging
import re

import yaml

from kontroll import catalog, gitio, paths
from kontroll.service.pending import promote_hint

log = logging.getLogger("kontroll.service.keygen")

# Global --config before the subcommand: sops discovers .sops.yaml by walking UP from cwd, which misses the
# overlay's subdir config (instance/.sops.yaml); updatekeys has no --config flag of its own, so it goes global.
SOPS_UPDATEKEYS = "sops --config instance/.sops.yaml updatekeys instance/secrets/*.sops.yml"

# The placeholder recipient shipped in instance.example/.sops.yaml. `kontroll-init --fresh` REPLACES every
# occurrence with the freshly-minted control key (seed_fresh_sops) — so a brand-new node names ONLY its own key,
# never inheriting another instance's recipients. Must match the literal in instance.example/.sops.yaml exactly.
FRESH_PLACEHOLDER = "age1exampleexampleexampleexampleexampleexampleexampleexa"

# An age PUBLIC-key recipient line inside a key_groups `age:` list: captures (indent, key). Quotes optional —
# sops/formatters may emit `- age1…` unquoted; the boundary scan must still see those as recipients so an
# insert never lands mid-block (defence-in-depth — the parse-verify gate is the authority either way). A
# trailing `# comment` is tolerated (the fresh-seed template leaves `# REPLACE` on the recipient line — without
# this the scanner would miss it and a later additive key-add would fail no_recipients on a fresh overlay).
_RECIPIENT_RE = re.compile(r'^(\s+)-\s*"?(age1[0-9a-z]+)"?\s*(?:#.*)?$')


def _sops_path(write=False):
    """The .sops.yaml path resolved against paths.ROOT DYNAMICALLY (so the tmp_repo fixture's repoint diverts
    keygen's read+write to the throwaway tree, like the other mutators). `write=True` for every WRITE (and for the
    read that precedes a write): a mutation must target the configured overlay/legacy file only — never the shipped
    instance.example/ placeholder a plain read may fall through to on an unconfigured checkout (paths.resolve)."""
    return paths.resolve(".sops.yaml", write=write)


def _read_sops(write=False):
    try:
        with open(_sops_path(write=write), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def generate_keypair():
    """Mint a fresh age keypair (gitio.age_keygen — the one shell-out seam) → {"public","private"} or None.
    A thin pass-through so `kontroll-init` (item E) and this dialog share ONE keygen primitive. The private key
    is held in memory only; this never logs it."""
    return gitio.age_keygen()


def _resolve_recipients(text):
    """Parse .sops.yaml text → {path_regex: set(age recipients)} with anchors/aliases EXPANDED (PyYAML
    resolves `*alias` to the `&anchor` value). This is the authoritative read used to PARSE-VERIFY that an
    edit only ADDED a recipient — it sees the real per-domain recipient set, regardless of the anchor sugar."""
    doc = yaml.safe_load(text) or {}
    out = {}
    for rule in doc.get("creation_rules", []) or []:
        keys = set()
        for grp in rule.get("key_groups", []) or []:
            for k in (grp or {}).get("age", []) or []:
                keys.add(k)
        out[rule.get("path_regex", "")] = keys
    return out


def _anchor_domains(text, anchor):
    """The domain basenames whose creation_rule uses `&anchor`/`*anchor` (so adding to that anchor block grants
    those domains) — derived from the TEXT, because the expanded parse (_resolve_recipients) loses which anchor
    a rule referenced. Human-facing only (the plan view); the authoritative grant is the parse-verify."""
    pr_re = re.compile(r"^\s*-\s*path_regex:.*?([A-Za-z0-9_]+)\\\.sops")
    kg_re = re.compile(r"^\s*key_groups:\s*[&*]%s\b" % re.escape(anchor))
    out, cur = [], None
    for ln in text.splitlines():
        m = pr_re.match(ln)
        if m:
            cur = m.group(1)
        elif kg_re.match(ln) and cur and cur not in out:
            out.append(cur)
    return out


def _add_recipient_to_anchor(text, anchor, public_key):
    """Insert `public_key` (an age1… PUBLIC key) into the `&<anchor>` recipient block of .sops.yaml text,
    after the last `- "age…"` entry at that entry's indentation. ANCHOR-PRESERVING (operates on text — a
    PyYAML round-trip would expand the anchors and bloat the file) and IDEMPOTENT (no-op if the key is already
    in that block). Returns (new_text, changed, error). error ∈ {anchor_not_found, no_recipients} | None."""
    lines = text.splitlines(keepends=True)
    anchor_re = re.compile(r"^\s*key_groups:\s*&%s\b" % re.escape(anchor))
    start = next((i for i, ln in enumerate(lines) if anchor_re.match(ln)), None)
    if start is None:
        return text, False, "anchor_not_found:%s" % anchor
    last_rec, indent, present = None, None, False
    for i in range(start + 1, len(lines)):
        m = _RECIPIENT_RE.match(lines[i])
        if m:
            last_rec, indent = i, m.group(1)
            if m.group(2) == public_key:
                present = True
            continue
        s = lines[i].strip()
        if s == "" or s.startswith("#"):
            continue                       # blank / comment lines stay inside the block scan
        if last_rec is not None:
            break                          # first non-recipient line after the recipients ends the block
    if last_rec is None:
        return text, False, "no_recipients:%s" % anchor
    if present:
        return text, False, None           # already a recipient — additive + idempotent
    lines.insert(last_rec + 1, '%s- "%s"\n' % (indent, public_key))
    return "".join(lines), True, None


def _intended_path_regexes(text, anchors):
    """The set of `path_regex` rule keys whose creation_rule references one of `anchors` (`&anchor` or
    `*anchor`) — i.e. the domains the edit is SUPPOSED to grant. Captured as the raw path_regex string, which
    equals the key PyYAML yields in _resolve_recipients (YAML plain scalars aren't unescaped), so the two sets
    are directly comparable. Used by the safety gate to confirm the key landed in EXACTLY these domains."""
    pr_re = re.compile(r"^\s*-\s*path_regex:\s*(\S+)")
    kg_re = re.compile(r"^\s*key_groups:\s*[&*](\w+)")
    out, cur = set(), None
    for ln in text.splitlines():
        m = pr_re.match(ln)
        if m:
            cur = m.group(1)
            continue
        k = kg_re.match(ln)
        if k and cur and k.group(1) in anchors:
            out.add(cur)
            cur = None
    return out


def _verify_additive(before, after, public_key, intended):
    """SAFETY GATE: confirm an edit was purely ADDITIVE and correctly TARGETED. Rejects unless: the domain set
    is unchanged; every prior recipient is still present (no drop → no unrecoverable secret); the only new
    recipient anywhere is `public_key` (no smuggled recipient); AND `public_key` was added to EXACTLY the
    `intended` domains — not fewer (under-grant) and not more (over-grant into a domain the role never named).
    Returns (ok, reason). This is the crown-jewel guard; on any reason apply writes nothing."""
    if set(before) != set(after):
        return False, "domain_set_changed"
    gained = set()
    for dom, prior in before.items():
        now = after.get(dom, set())
        if not prior <= now:
            return False, "recipient_dropped"
        added = now - prior
        if added - {public_key}:
            return False, "unexpected_recipient"
        if public_key in added:
            gained.add(dom)
    if gained != intended:
        return False, "wrong_domains"            # over- or under-granted vs the role's recipient_groups
    return True, None


def _next_steps(desc, run_id=None, staged=True):
    """The DEFERRED operator steps after a generate — placement of the show-once key, promote, the high-blast
    re-wrap (never auto-run), and (rotation) retiring the old recipient. Shown in the plan AND the result so
    the operator knows the follow-on before and after generating. The plan has no run id yet (a placeholder
    stands in); the result names the one the route minted (T19 — it used to print the placeholder there too), and
    has no promote step when nothing was staged."""
    steps = []
    if desc.get("placement") == "offline":
        steps.append("Store the shown private key OFFLINE on trusted media — it is not saved here.")
    else:
        steps.append("Save the shown private key to %s on the control VM (mode 0600)." % desc.get("private_key_path"))
    if staged:
        steps.append("So the new recipient lands on main, %s." % promote_hint(run_id or "<run_id>"))
    steps.append("Re-wrap existing secrets to the new recipient (operator-run on a host holding a valid key; "
                 "NOT run automatically): `%s`." % SOPS_UPDATEKEYS)
    if desc.get("role") == "control":
        steps.append("After verifying the new key decrypts, remove the OLD control recipient from /.sops.yaml "
                     "and re-wrap once more to complete the rotation.")
    return steps


def _plan_from(desc, text):
    """The client-safe descriptor view (NO key material — none exists at plan time): label/scope/placement +
    the domains the public key will become a recipient of (derived from .sops.yaml) + the deferred steps."""
    domains = []
    for anchor in desc.get("recipient_groups", []) or []:
        for d in _anchor_domains(text or "", anchor):
            if d not in domains:
                domains.append(d)
    return {"error": None, "role": desc["role"], "label": desc.get("label", desc["role"]),
            "description": desc.get("description", ""), "placement": desc.get("placement", "offline"),
            "private_key_path": desc.get("private_key_path"),
            "recipient_groups": list(desc.get("recipient_groups", []) or []),
            "recipient_domains": domains, "scope_label": desc.get("scope_label", ""),
            "high_blast": bool(desc.get("high_blast")), "warning": desc.get("warning", ""),
            "next_steps": _next_steps(desc)}


def build_keygen_plan(role):
    """PURE: resolve WHAT generating `role`'s key will do, WITHOUT generating it (no private key exists yet, so
    the whole plan is client-safe). Returns the descriptor view (see _plan_from) or {"error": "no_role"}. The
    route renders this so the operator sees the scope + the deferred re-wrap BEFORE minting anything."""
    desc = catalog.key_role(role)
    if desc is None:
        return {"error": "no_role"}
    return _plan_from(desc, _read_sops())


def next_steps(role, run_id=None, staged=True):
    """`role`'s deferred steps for a result whose staging outcome is known (the route's; `staged` False drops the
    promote step), or [] for an unknown role."""
    desc = catalog.key_role(role)
    return _next_steps(desc, run_id, staged) if desc is not None else []


def apply_keygen_plan(role, run_id=None):
    """Mint `role`'s keypair, add the PUBLIC recipient to its /.sops.yaml anchor group(s) (additive, idempotent,
    PARSE-VERIFIED no-drop), and return the result INCLUDING the show-once `private_key` (the ONLY place it ever
    appears — the route returns it once; it is never logged/committed/audited). The .sops.yaml change is the
    only repo write (committed + STAGED by the route, C10). Returns {"error": None|<code>, "changed", "role",
    "public_key", "private_key", "placement", "private_key_path", "recipient_domains", "high_blast",
    "sops_updatekeys", "next_steps", "paths": [paths.overlay_rel(".sops.yaml")]}. error codes: no_role | no_recipient_groups |
    no_sops_config | keygen_failed | <anchor error> | verify_failed:<reason> — on any error NOTHING is written
    and no key is returned (the misconfig guards return before a key is even minted). Its `next_steps` assume the
    write will be staged; the GUI route answers `next_steps(role, run_id, staged=…)` instead, once it knows."""
    desc = catalog.key_role(role)
    if desc is None:
        return {"error": "no_role", "changed": False}
    anchors = list(desc.get("recipient_groups", []) or [])
    if not anchors:
        return {"error": "no_recipient_groups", "changed": False}   # never mint a key with nowhere to grant it
    if _read_sops(write=True) is None:                   # about to write: the configured file must exist
        return {"error": "no_sops_config", "changed": False}        # guard BEFORE minting (no wasted key)
    pair = generate_keypair()
    if not pair:
        return {"error": "keygen_failed", "changed": False}
    public, private = pair["public"], pair["private"]
    w = add_recipient(public, anchors)                              # the shared parse-verified writer
    if w["error"]:
        return {"error": w["error"], "changed": False}             # never returns the key on an error path
    return {"error": None, "role": role, "public_key": public, "private_key": private,
            "placement": desc.get("placement", "offline"), "private_key_path": desc.get("private_key_path"),
            "recipient_domains": w["recipient_domains"], "high_blast": bool(desc.get("high_blast")),
            "sops_updatekeys": SOPS_UPDATEKEYS, "next_steps": _next_steps(desc, run_id),
            "paths": [paths.overlay_rel(".sops.yaml")],
            "changed": w["changed"]}


_DOMAIN_RE = re.compile(r"path_regex:.*?([A-Za-z0-9_]+)\\\.sops")


def _all_domains(text):
    """The domain basenames named by the creation_rule path_regexes (network, proxmox, …), in file order. The
    catch-all (`.*\\.sops…`) has no word-char basename and is skipped. Human-facing only (the result view)."""
    out = []
    for ln in text.splitlines():
        m = _DOMAIN_RE.search(ln)
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def seed_fresh_sops(public_key):
    """FRESH-instance recipient seed — the INVERSE of add_recipient's additive model. Writes the overlay
    .sops.yaml naming ONLY `public_key`, by substituting the shipped template's FRESH_PLACEHOLDER recipient. A
    brand-new node must NOT inherit another instance's recipients (the whole point of `--fresh`), so this
    REPLACES rather than appends. Operates on the resolved overlay path (instance/.sops.yaml once the overlay dir
    exists — kontroll-init --fresh scaffolds it from instance.example/ first). IDEMPOTENT: re-running after the
    placeholder is already replaced (recipients == {public_key}) is a no-op. REFUSES (error=already_configured)
    if the file names real recipients and has NO placeholder — never clobber a configured overlay. The SAFETY
    GATE (post-substitution every domain names EXACTLY {public_key}) is the crown-jewel guard: on failure
    NOTHING is written. Returns {"error", "changed", "paths": [paths.overlay_rel(".sops.yaml")], "recipient_domains": [...]}.
    error ∈ {no_sops_config | already_configured | seed_verify_failed} | None."""
    text = _read_sops(write=True)                        # read the file we are about to WRITE (never the example)
    if text is None:
        return {"error": "no_sops_config", "changed": False, "paths": [paths.overlay_rel(".sops.yaml")], "recipient_domains": []}
    if FRESH_PLACEHOLDER not in text:
        recips = set().union(*_resolve_recipients(text).values()) if _resolve_recipients(text) else set()
        if recips == {public_key}:                       # already seeded with exactly this key — idempotent
            return {"error": None, "changed": False, "paths": [paths.overlay_rel(".sops.yaml")],
                    "recipient_domains": _all_domains(text)}
        return {"error": "already_configured", "changed": False, "paths": [paths.overlay_rel(".sops.yaml")], "recipient_domains": []}
    new_text = text.replace(FRESH_PLACEHOLDER, public_key)
    # The template marks each placeholder recipient line with a trailing "# REPLACE"; once it holds the real key
    # that marker is stale AND confusing (it reads as "still a placeholder"). Strip it so the seeded file is
    # clean — defence-in-depth with the comment-tolerant _RECIPIENT_RE above.
    new_text = re.sub(r'(-\s*"?%s"?)[ \t]*#[ \t]*REPLACE[ \t]*$' % re.escape(public_key), r"\1", new_text, flags=re.M)
    after = _resolve_recipients(new_text)
    all_recips = set().union(*after.values()) if after else set()
    if all_recips != {public_key}:                       # never leave a stray/foreign recipient in a fresh tree
        return {"error": "seed_verify_failed", "changed": False, "paths": [paths.overlay_rel(".sops.yaml")], "recipient_domains": []}
    with open(_sops_path(write=True), "w", encoding="utf-8") as fh:
        fh.write(new_text)
    return {"error": None, "changed": True, "paths": [paths.overlay_rel(".sops.yaml")], "recipient_domains": _all_domains(new_text)}


def add_recipient(public_key, anchors):
    """Add `public_key` (an age PUBLIC key) to the named /.sops.yaml anchor group(s) — the additive,
    idempotent, PARSE-VERIFIED recipient write shared by apply_keygen_plan (a freshly-minted key) and
    `kontroll-init` (the EXISTING control key). Reads + writes /.sops.yaml under paths.ROOT. Returns
    {"error": None|no_sops_config|<anchor error>|verify_failed:<reason>, "changed", "paths": [paths.overlay_rel(".sops.yaml")],
    "recipient_domains": [...]}. On ANY error NOTHING is written (the crown-jewel additive guard)."""
    text = _read_sops(write=True)                        # read the file we are about to WRITE (never the example)
    if text is None:
        return {"error": "no_sops_config", "changed": False, "paths": [paths.overlay_rel(".sops.yaml")], "recipient_domains": []}
    before = _resolve_recipients(text)
    new_text, changed_any = text, False
    for anchor in anchors:
        new_text, changed, err = _add_recipient_to_anchor(new_text, anchor, public_key)
        if err:
            return {"error": err, "changed": False, "paths": [paths.overlay_rel(".sops.yaml")], "recipient_domains": []}
        changed_any = changed_any or changed
    domains = []
    for anchor in anchors:
        for d in _anchor_domains(text, anchor):
            if d not in domains:
                domains.append(d)
    if not changed_any:                                   # already a recipient — idempotent no-op
        return {"error": None, "changed": False, "paths": [paths.overlay_rel(".sops.yaml")], "recipient_domains": domains}
    intended = _intended_path_regexes(text, set(anchors))
    ok, why = _verify_additive(before, _resolve_recipients(new_text), public_key, intended)
    if not ok:
        return {"error": "verify_failed:%s" % why, "changed": False, "paths": [paths.overlay_rel(".sops.yaml")],
                "recipient_domains": []}
    with open(_sops_path(write=True), "w", encoding="utf-8") as fh:
        fh.write(new_text)
    return {"error": None, "changed": True, "paths": [paths.overlay_rel(".sops.yaml")], "recipient_domains": domains}
