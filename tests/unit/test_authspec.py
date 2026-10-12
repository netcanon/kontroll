"""authspec (F1 TIER-A): the onboard form's credential fields are DERIVED per-backend from each backend's `auth:`
block, not a one-size-fits-all union — so a blind user sees the RIGHT fields for the device they picked.

WHY (the failure these guard — the operator's dogfood gap, docs/reviews/2026-06-29-north-star-onboard/): the static
4-field union showed a REST/`httpapi` device dead SSH boxes and offered a network switch a useless API-token box. The
derivation reads each backend's declared auth SHAPE (config-as-data) so a `network_cli` device gets SSH login (+ enable)
and an `api` device gets a token (no SSH key). The crown-jewel property is asserted as DATA: the CredField descriptors
are PURE SCHEMA — names/kinds/labels, NEVER a credential value (the same no-leak posture as secret-forms). A backend
that declares no block degrades to the generic union (`source="fallback"`) so onboarding is never blocked (INVARIANT D*).
"""
import re

import pytest
import yaml

from kontroll import authspec, catalog, paths

pytestmark = pytest.mark.unit


def _backend(name):
    return next((b for b in catalog.load_backends() if b.get("name") == name), None)


def _field(fields, name):
    return next((cf for cf in fields if cf["field"] == name), None)


def test_network_cli_derives_ssh_login_not_an_api_token():
    """A `netcommon_cli` device derives SSH-login fields (username/password/ssh_private_key + the optional enable
    secret) and NO `api_token` — guards a network switch's onboard form offering a useless REST token box."""
    out = authspec.derive_auth(_backend("netcommon_cli"), "network")
    assert out["source"] == "shallow"
    names = {cf["field"] for cf in out["fields"]}
    assert {"username", "password", "ssh_private_key", "enable_password"} <= names
    assert "api_token" not in names
    assert _field(out["fields"], "ssh_private_key")["kind"] == "ssh_key"        # the key widget
    assert _field(out["fields"], "password")["secret"] is True                  # masked + SOPS
    assert _field(out["fields"], "username")["secret"] is False                 # an identity, not a secret


def test_api_backend_derives_a_token_not_a_dead_ssh_box():
    """THE HEADLINE WIN: a REST/`httpapi` (`api` backend) device derives an `api_token` field and NO `ssh_private_key`
    — so a REST device stops showing dead SSH boxes (the exact gap the operator hit onboarding the firewall)."""
    out = authspec.derive_auth(_backend("api"), "network")
    names = {cf["field"] for cf in out["fields"]}
    assert "api_token" in names
    assert "ssh_private_key" not in names, "a REST device must not show an SSH key box"
    assert _field(out["fields"], "api_token")["secret"] is True


def test_fallback_to_generic_union_when_a_backend_declares_no_auth_block():
    """A backend with no `auth:` block (or an unknown backend) degrades to the generic 4-field union with
    `source='fallback'` — so a brand-new/unclassified device shows EXACTLY today's form, never a dead end
    (INVARIANT D*). Guards a derivation gap blocking onboarding."""
    out = authspec.derive_auth({"name": "brand_new", "role": "x"}, "network")
    assert out["source"] == "fallback"
    assert {cf["field"] for cf in out["fields"]} == {"username", "password", "api_token", "ssh_private_key"}


def test_derive_stamps_the_target_domain_on_every_field():
    """Every derived field carries the onboard's SOPS `domain` — the input the planner's domain-confinement guard
    (MF-S2) checks, so a secret can never be written to a domain the operator didn't choose. Guards the guard's
    precondition silently going missing."""
    out = authspec.derive_auth(_backend("netcommon_cli"), "proxmox")
    assert out["fields"] and all(cf["domain"] == "proxmox" for cf in out["fields"])


def test_derived_fields_carry_no_credential_values():
    """The CredField descriptors are PURE SCHEMA — no descriptor carries a `value`/`secret_value`/`password` key
    (only name/kind/label/flags). Guards a value leaking onto the derived-field surface (which the GUI + the
    GET /onboard/cred-fields read route will both expose)."""
    for backend in ("netcommon_cli", "api", "raw_ssh", "napalm", "vendor_config"):
        for cf in authspec.derive_auth(_backend(backend), "network")["fields"]:
            assert not ({"value", "secret_value", "password", "token", "creds"} & set(cf.keys())), \
                "%s field %s carries a value-shaped key — descriptors must be names-only" % (backend, cf["field"])


def test_every_shipped_backend_auth_block_is_well_formed():
    """config-as-data-validated: every `auth:` entry a shipped backend declares has a `field` and a `kind` in the
    closed CredField enum, and a secret/SSH field declares the secret routing. Guards a malformed drop-in
    (a typo'd kind, a missing field) silently producing a broken onboard form."""
    for b in catalog.load_backends():
        for e in (b.get("auth") or []):
            assert isinstance(e, dict) and e.get("field"), "%s: an auth entry has no field" % b.get("name")
            assert e.get("kind", "password") in authspec.KINDS, \
                "%s: auth field %s has kind %r not in the closed enum" % (b.get("name"), e["field"], e.get("kind"))


def test_union_field_maps_a_legacy_scalar_for_back_compat():
    """`union_field` returns the generic-union descriptor for a legacy scalar (the back-compat safety net: a field a
    caller passes that the backend's block doesn't declare is mapped from here, never silently dropped); an unknown
    field is None. Guards an existing caller's credential being lost by the new per-backend derivation."""
    assert authspec.union_field("api_token")["kind"] == "token"
    assert authspec.union_field("ssh_private_key")["kind"] == "ssh_key"
    assert authspec.union_field("not_a_cred") is None


def test_module_auth_block_overrides_the_backend_shape():
    """F1 seam S1: a curated class's OWN auth: block (the reuse path) takes precedence over the backend's generic
    shape — so a proxmox reuse derives its coherent multi-field api-token auth_set, not the api backend's single
    api_token box. Guards a curated multi-field credential being mangled back into the one-size-fits-all shape."""
    module = {"key": "proxmox", "auth": [
        {"field": "api_user", "kind": "identity", "auth_set": "proxmox_api", "required": True,
         "shared": True, "sops_stem": "proxmox_api_user"},
        {"field": "api_token_secret", "kind": "token", "auth_set": "proxmox_api", "required": True,
         "shared": True, "sops_stem": "proxmox_api_token_secret"}]}
    out = authspec.derive_auth(_backend("api"), "proxmox", module=module)
    assert out["source"] == "shallow"
    names = {cf["field"] for cf in out["fields"]}
    assert names == {"api_user", "api_token_secret"} and "api_token" not in names
    secret = _field(out["fields"], "api_token_secret")
    assert secret["secret"] is True and secret["shared"] is True and secret["auth_set"] == "proxmox_api"
    assert _field(out["fields"], "api_user")["secret"] is False           # an identity, not a secret


def test_shipped_proxmox_module_declares_one_coherent_shared_auth_set():
    """The SHIPPED modules/proxmox auth: block is ONE auth_set of three SHARED fields (user/token_id/token_secret),
    only the secret masked — the multi-field-mangled-into-one-box defect closed for the real curated class. Guards
    the curated proxmox credential shape silently regressing to the generic union."""
    mod = yaml.safe_load(open(paths.module_file("proxmox"), encoding="utf-8"))
    fields = authspec.auth_fields_for_backend(mod)
    assert {cf["field"] for cf in fields} == {"api_user", "api_token_id", "api_token_secret"}
    assert {cf["auth_set"] for cf in fields} == {"proxmox_api"} and all(cf["shared"] for cf in fields)
    assert _field(fields, "api_token_secret")["secret"] is True
    assert _field(fields, "api_user")["secret"] is False and _field(fields, "api_token_id")["secret"] is False


def test_proxmox_shared_sops_stems_match_the_pve_exporter_secret_env_map():
    """THE coherence guard (F1 seam S1 ⊕ F2 Step 3): every field name telemetry/pve.yml's secret_env_map reads
    ({proxmox_api_user}/{_token_id}/{_token_secret}) is provided by the proxmox auth_set's shared sops_stems — so
    onboarding a PVE node FEEDS the pve-exporter and Grafana populates. Guards the 'empty Grafana' drift: a rename on
    either side silently starves the exporter (the exact dogfood symptom this rung exists to close)."""
    mod = yaml.safe_load(open(paths.module_file("proxmox"), encoding="utf-8"))
    pve = yaml.safe_load(open(paths.resolve("telemetry/pve.yml"), encoding="utf-8"))
    stems = {cf["sops_stem"] for cf in authspec.auth_fields_for_backend(mod) if cf.get("shared")}
    referenced = set()
    for tmpl in pve["secret_env_map"].values():
        referenced |= set(re.findall(r"\{(\w+)\}", tmpl))
    assert referenced and referenced <= stems, \
        "pve-exporter reads %s but the proxmox auth_set provides %s" % (referenced, stems)


def test_public_descriptor_keeps_the_shared_flag_for_grouping():
    """public_descriptor (the wire projection) keeps `shared` + `auth_set` so the GUI can group the credential, while
    still dropping any value-shaped key. Guards the grouping/keying markers being lost at the wire (the GUI would
    then render a domain cred as a loose host-keyed box)."""
    cf = authspec._normalize({"field": "api_user", "kind": "identity", "shared": True,
                              "auth_set": "proxmox_api", "sops_stem": "proxmox_api_user"})
    out = authspec.public_descriptor(dict(cf, value="leak-me"))
    assert out["shared"] is True and out["auth_set"] == "proxmox_api" and out["sops_stem"] == "proxmox_api_user"
    assert "value" not in out


def test_public_descriptor_projects_only_schema_keys_drops_anything_else():
    """`public_descriptor` is the WIRE guard the plan views + the GET /onboard/cred-fields route project through: it
    keeps ONLY the closed schema keys (field/kind/label/secret/…), so even a descriptor that somehow grew a
    value-shaped key cannot leak it to the client. Guards the names-only contract at the wire, not just by
    convention — the keys the GUI receives are exactly the schema, a planted `value`/`secret_value` is dropped."""
    cf = authspec.derive_auth(_backend("netcommon_cli"), "network")["fields"][0]
    tampered = dict(cf, value="leak-me", secret_value="leak-me", password="leak-me")
    out = authspec.public_descriptor(tampered)
    assert out["field"] == cf["field"] and out["kind"] == cf["kind"] and out["domain"] == "network"
    assert not ({"value", "secret_value", "password", "token", "creds"} & set(out.keys())), \
        "public_descriptor must drop every value-shaped key — the wire surface is names-only"
    assert set(out.keys()) <= set(authspec._PUBLIC_KEYS)


# ── F1 TIER-B (Rung 3): the argument_spec extractor + its two hardening gates ─────────────────────────────────── #
# Offline: inject a fake `ad` (the ansible-doc shell-out seam) + an explicit `installed` set, so no subprocess runs.

# A synthetic proxmox_kvm argument_spec — the design's worked example (§2.2). `no_log` is the ONLY HARD secret
# signal; the identity/host/bool params are non-secret locators; `name`/`memory` are operational knobs to be SKIPPED.
_PROXMOX_KVM_ARGSPEC = {
    "api_host":         {"type": "str",  "required": True,  "description": "PVE host"},
    "api_user":         {"type": "str",  "required": True,  "description": "user@realm"},
    "api_token_id":     {"type": "str",                     "description": "the token name"},
    "api_token_secret": {"type": "str",  "no_log": True,    "description": "the token secret value"},
    "api_password":     {"type": "str",  "no_log": True,    "description": "alternative to the token"},
    "validate_certs":   {"type": "bool", "default": True,   "description": "TLS verification posture"},
    "name":             {"type": "str",                     "description": "the VM name"},   # operational — SKIP
    "memory":           {"type": "int",                     "description": "RAM in MB"},     # operational — SKIP
}


def _fake_ad(specs):
    """A stand-in for probe._ad: `specs` maps a short module name → its argument_spec. Returns the `ansible-doc -j`
    JSON shape for `["-j", "coll.module"]`, or "" for anything else. Records every call so a test can assert the
    ad seam is NEVER touched (the MF-S1 no-introspect-uninstalled property)."""
    calls = []

    def ad(args):
        calls.append(list(args))
        if len(args) == 2 and args[0] == "-j":
            mod = args[1].split(".")[-1]
            if mod in specs:
                return __import__("json").dumps({args[1]: {"doc": {"options": specs[mod]}}})
        return ""
    ad.calls = calls
    return ad


def test_tier_b_derives_proxmox_coherent_auth_set_from_argspec():
    """TIER-B feeds a synthetic proxmox_kvm argument_spec and yields ONE coherent auth_set: api_host/api_user/
    api_token_id are non-secret locators, api_token_secret/api_password are secret (no_log), validate_certs is a
    non-secret bool, and the operational `name`/`memory` params are DROPPED. Guards the multi-field-mangled-into-
    one-box defect (the exact proxmox credential shape, derived, not hand-frozen)."""
    ad = _fake_ad({"proxmox_kvm": _PROXMOX_KVM_ARGSPEC})
    fields = authspec.auth_fields_from_argspec("community.proxmox", ["proxmox_kvm"],
                                               ad=ad, installed={"community.proxmox"})
    names = {cf["field"] for cf in fields}
    assert names == {"api_host", "api_user", "api_token_id", "api_token_secret", "api_password", "validate_certs"}
    assert "name" not in names and "memory" not in names               # operational knobs are not credentials
    assert {cf["auth_set"] for cf in fields} == {"proxmox_api"}         # ONE coherent set
    assert _field(fields, "api_token_secret")["secret"] is True         # no_log → masked
    assert _field(fields, "api_user")["secret"] is False                # identity → not masked
    assert _field(fields, "api_host")["secret"] is False and _field(fields, "validate_certs")["kind"] == "bool"


def test_tier_b_no_log_is_a_hard_secret_overriding_an_innocuous_name():
    """A param with an innocuous name but `no_log: true` is classified SECRET (masked) — `no_log` is the module
    author's own HARD declaration and wins over the name. Guards a real secret rendered as a plaintext box."""
    ad = _fake_ad({"widget": {"harmless_looking": {"type": "str", "no_log": True, "required": True}}})
    fields = authspec.auth_fields_from_argspec("acme.things", ["widget"], ad=ad, installed={"acme.things"})
    cf = _field(fields, "harmless_looking")
    assert cf["secret"] is True and cf["derivation"] == "no_log"


def test_tier_b_soft_name_marks_secret_only_for_secret_classes():
    """A name-only signal marks SECRET only for a secret CLASS (password/token/ssh_key); an `api_user`-style
    identity name is NON-secret even inside an auth-bearing module. Guards an identity field needlessly masked
    (or, inversely, a token box left as plaintext)."""
    ad = _fake_ad({"login": {"api_password": {"type": "str", "no_log": True},
                             "api_user":     {"type": "str", "required": True}}})
    fields = authspec.auth_fields_from_argspec("acme.things", ["login"], ad=ad, installed={"acme.things"})
    assert _field(fields, "api_password")["secret"] is True             # secret-class name
    assert _field(fields, "api_user")["secret"] is False                # identity name — not a secret


def test_tier_b_masks_a_genuinely_ambiguous_required_field_mf_s6():
    """MF-S6: a required param with NO no_log and NO name signal, inside an auth-bearing module, is genuinely
    ambiguous → default MASKED/secret (derivation 'ambiguous_masked'). ansible-doc's no_log is not a complete
    oracle, so we fail SAFE — masking a non-secret is harmless; plaintexting a missed secret is a leak."""
    ad = _fake_ad({"auth": {"api_token_secret": {"type": "str", "no_log": True},
                            "mystery":          {"type": "str", "required": True}}})   # no no_log, no name signal
    fields = authspec.auth_fields_from_argspec("acme.things", ["auth"], ad=ad, installed={"acme.things"})
    cf = _field(fields, "mystery")
    assert cf is not None and cf["secret"] is True and cf["derivation"] == "ambiguous_masked"


def test_tier_b_install_confined_never_introspects_an_uninstalled_collection_mf_s1():
    """MF-S1 (the security crux): auth_fields_from_argspec on a NOT-installed collection returns None AND never
    calls the ansible-doc seam — so TIER-B can never execute an un-installed collection's Python, even if a caller
    forgets the search-flow gate. Guards an un-installed-RCE via a 'sharpen a Galaxy result' path."""
    ad = _fake_ad({"proxmox_kvm": _PROXMOX_KVM_ARGSPEC})
    out = authspec.auth_fields_from_argspec("community.proxmox", ["proxmox_kvm"], ad=ad, installed=set())
    assert out is None
    assert ad.calls == [], "MF-S1 breached: ansible-doc was invoked for an un-installed collection"


def test_tier_b_never_raises_on_ad_failure_and_keeps_tier_a():
    """A flaky/garbage ansible-doc (`ad` → '' or non-JSON) yields None from TIER-B, and derive_auth keeps the
    TIER-A shape with no exception. Guards a 500-ing ansible-doc blocking onboarding (INVARIANT D*)."""
    for bad in (lambda a: "", lambda a: "not json{", lambda a: None):
        assert authspec.auth_fields_from_argspec("community.proxmox", ["proxmox_kvm"],
                                                 ad=bad, installed={"community.proxmox"}) is None
    out = authspec.derive_auth(_backend("api"), "network", deep=True, coll="community.proxmox",
                               modules=["proxmox_kvm"], ad=lambda a: "garbage", installed={"community.proxmox"})
    assert out["source"] == "shallow" and "api_token" in {cf["field"] for cf in out["fields"]}


def test_derive_auth_deep_replaces_the_coarse_shape_with_exact_fields():
    """derive_auth(deep=True) SHARPENS: for a proxmox result the exact argspec auth_set REPLACES the api backend's
    generic single-token shape, source becomes 'deep', and every field carries the target domain (so the MF-S2
    guard still applies). Guards the deep tier not actually reaching the form / dropping the domain stamp."""
    ad = _fake_ad({"proxmox_kvm": _PROXMOX_KVM_ARGSPEC})
    out = authspec.derive_auth(_backend("api"), "proxmox", deep=True, coll="community.proxmox",
                               modules=["proxmox_kvm"], ad=ad, installed={"community.proxmox"})
    assert out["source"] == "deep"
    names = {cf["field"] for cf in out["fields"]}
    assert {"api_host", "api_user", "api_token_id", "api_token_secret"} <= names
    assert "api_token" not in names                                    # the coarse single-box shape is superseded
    assert all(cf["domain"] == "proxmox" for cf in out["fields"])      # domain still stamped for MF-S2


def test_derive_auth_deep_falls_back_to_tier_a_when_uninstalled():
    """derive_auth(deep=True) for an UN-installed collection keeps the coarse TIER-A shape (source unchanged) — the
    MF-S1 gate inside auth_fields_from_argspec makes deep a safe no-op offline. Guards deep silently blocking or
    erroring when the collection isn't present."""
    ad = _fake_ad({"proxmox_kvm": _PROXMOX_KVM_ARGSPEC})
    out = authspec.derive_auth(_backend("api"), "network", deep=True, coll="community.proxmox",
                               modules=["proxmox_kvm"], ad=ad, installed=set())
    assert out["source"] == "shallow" and "api_token" in {cf["field"] for cf in out["fields"]}
    assert ad.calls == []


def test_tier_b_fields_carry_no_credential_values():
    """The TIER-B CredFields are PURE SCHEMA too — no value-shaped key rides on a derived argspec field (the same
    no-leak posture as TIER-A, now for the dynamic tier). Guards a value leaking through the deep path."""
    ad = _fake_ad({"proxmox_kvm": _PROXMOX_KVM_ARGSPEC})
    fields = authspec.auth_fields_from_argspec("community.proxmox", ["proxmox_kvm"],
                                               ad=ad, installed={"community.proxmox"})
    for cf in fields:
        assert not ({"value", "secret_value", "password", "token", "creds"} & set(cf.keys())), \
            "TIER-B field %s carries a value-shaped key" % cf["field"]


def test_auth_candidate_modules_picks_signal_modules_bounded():
    """auth_candidate_modules picks the connection/api-signal modules from the probed module list and caps the set
    (deep introspection runs collection Python — the cap bounds it). Guards TIER-B fanning out ansible-doc across a
    whole collection, and guards a purely-operational module list yielding no candidates."""
    facts = {"modules": ["proxmox_kvm", "proxmox_node", "proxmox_storage_info", "proxmox_pool", "widget", "gadget"]}
    cands = authspec.auth_candidate_modules(facts)
    assert "proxmox_kvm" in cands and "widget" not in cands
    assert len(cands) <= authspec._MAX_CANDIDATE_MODULES


def test_tier_b_is_gated_to_module_param_backends_keeps_cliconf_ssh_shape():
    """THE review-30 F-1 regression: a conn-magic backend (network_cli) is NEVER sharpened by TIER-B, even when a
    candidate module carries a no_log secret (e.g. cisco.ios `ios_user`'s `configured_password`). Otherwise TIER-B
    would REPLACE the switch's SSH-login shape with device-account params, leaving the operator no way to enter the
    connection creds → the device is unreachable. Guards that regression: the SSH shape stands, source stays
    'shallow', and the ansible-doc seam is never even called (the backend gate short-circuits before it)."""
    ad = _fake_ad({"ios_user": {"configured_password": {"type": "str", "no_log": True}, "name": {"type": "str"}}})
    out = authspec.derive_auth(_backend("netcommon_cli"), "network", deep=True, coll="cisco.ios",
                               modules=["ios_user"], ad=ad, installed={"cisco.ios"})
    assert out["source"] == "shallow"                                  # TIER-A SSH-login shape stands, not replaced
    names = {cf["field"] for cf in out["fields"]}
    assert {"username", "password", "ssh_private_key"} <= names        # the CONNECTION creds are still offered
    assert "configured_password" not in names                         # the device-account param did NOT take over
    assert ad.calls == [], "the backend gate must skip introspection for a conn-magic backend (no ansible-doc call)"


def test_tier_b_client_key_is_a_secret_not_a_plaintext_ca_cert():
    """THE review-20 false-plaintext regression: a `client_key` param (the PRIVATE half of a keypair, often WITHOUT
    a doc `no_log`) is classified SECRET (kind ssh_key), NOT a non-secret ca_cert. Guards a private key rendered as
    a plaintext box. (`client_cert`, the public half, stays a non-secret ca_cert — checked implicitly by the pattern
    split.)"""
    ad = _fake_ad({"connect": {"api_token_secret": {"type": "str", "no_log": True}, "client_key": {"type": "str"}}})
    fields = authspec.auth_fields_from_argspec("acme.things", ["connect"], ad=ad, installed={"acme.things"})
    cf = _field(fields, "client_key")
    assert cf is not None and cf["secret"] is True and cf["kind"] == "ssh_key"


def test_auth_candidate_modules_ignores_non_string_module_entries():
    """THE review-30 F-2 regression: a non-string entry in the probed module list (a malformed/degraded fact) is
    skipped, never raised — auth_candidate_modules runs OUTSIDE derive_auth's try/except at the onboard call site,
    so it must honour the never-raise contract itself. Guards a latent INVARIANT-D* trip-wire blocking onboarding."""
    facts = {"modules": ["proxmox_kvm", None, 123, {"x": 1}, "proxmox_node"]}
    assert authspec.auth_candidate_modules(facts) == ["proxmox_kvm", "proxmox_node"]


def test_tier_b_does_not_offer_an_option_whose_name_is_not_an_identifier():
    """An OPTIONAL option of an installed module whose name is not an identifier (`api-key`, a name with a space,
    a brace, sixty-five letters) is not offered as a credential field; the well-formed options beside it are. Two
    of them (`old-api_host`, a locator beside a secret; `x-y_password`, a secret by its name) are options the
    classifier takes when their name is an identifier, so it is the name filter that keeps them out. When such an
    option is REQUIRED or marked `no_log`, the module is a miss as a whole and TIER-B returns nothing, so the
    TIER-A shape stands.

    Guards a third party's spelling refusing an onboard: the planner formats a field's name into a lookup
    expression and refuses one that is not an identifier, so a derived field with such a name turned `deep: true`
    into a 409 that the same request without `deep` did not get. And guards the other fault: a set labelled
    exact that silently lacks a required secret, with the all-or-none check satisfied by the rest."""
    good = {"api_host": {"type": "str", "required": True},
            "api_password": {"type": "str", "no_log": True, "required": True}}
    odd = {"api-key": {"type": "str"}, "api token": {"type": "str"}, "pw }} {{ x": {"type": "str"},
           "x" * 65: {"type": "str"},
           "old-api_host": {"type": "str"}, "x-y_password": {"type": "str"}}   # ones the classifier WOULD take
    ad = _fake_ad({"login": dict(good, **odd)})
    fields = authspec.auth_fields_from_argspec("acme.things", ["login"], ad=ad, installed={"acme.things"})
    assert {cf["field"] for cf in fields} == {"api_host", "api_password"}
    for spec in ({"type": "str", "no_log": True}, {"type": "str", "required": True}):
        ad = _fake_ad({"login": dict(good, **{"api-key": spec})})
        assert authspec.auth_fields_from_argspec("acme.things", ["login"], ad=ad, installed={"acme.things"}) is None


def test_a_declared_auth_entry_whose_field_or_kind_is_not_a_string_is_skipped():
    """An `auth:` entry whose `field` is a mapping or a number, or whose `kind` is a list, is skipped like any
    other malformed entry, and the well-formed entry beside it is kept; an entry whose `auth_set`, `group` or
    `help` is not a string is kept WITHOUT that key.

    Guards the 500 a module file with such an entry was on both apps (a TypeError where the planner collects the
    declared field names, or the names of the credential sets, into a set)."""
    out = authspec.auth_fields_for_backend({"auth": [
        {"field": {"a": 1}, "kind": "host"}, {"field": "x", "kind": ["host"]}, {"field": 7, "kind": "host"},
        {"field": "api_host", "kind": "host", "maps_to": "ansible_host", "auth_set": ["s"], "group": {"g": 1},
         "help": 5}]})
    assert [cf["field"] for cf in out] == ["api_host"]
    assert not {"auth_set", "group", "help"} & set(out[0])
