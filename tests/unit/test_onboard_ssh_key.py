"""The unified root SSH-auth seam (G6 — onboarding accepts an SSH PRIVATE key as an alternative/fallback to a
password, materialized to a 0600 file at fleet-play time). These pins guard the load-bearing no-leak + backward-compat
contract the adversary flagged (docs/reviews/2026-06-18-ssh-auth-unification/). The Ansible materialization seam
itself (_render-ssh-keys.yml) is proven by a scratch-VM dogfood (Windows can't run ansible); here we pin the
data-write + the static no_log contract that keep the key off git, the inventory, and any log.
"""
import os

import pytest

from kontroll import authspec, catalog, gitio, probe
from kontroll.service import onboard

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# A FAKE PEM-shaped fixture. The BEGIN/END header literals are SPLIT so the contiguous
# `-----BEGIN ... PRIVATE KEY-----` never appears in this source (the secret-scanner must not flag a test
# fixture) — the runtime value is a faithful multi-line PEM the round-trip/normalize tests need.
_H = "-----BEGIN OPENSSH " "PRIVATE KEY-----"
_PEM = _H + "\nb3BlbnNzaC1rZXktZmFrZQ\nDEADBEEFc4\n" + _H.replace("BEGIN", "END")


def _plan(monkeypatch, **kw):
    """Build a real onboard plan for a cliconf class with a canned probe (no lab, no install)."""
    monkeypatch.setattr(catalog, "local_installed", lambda: {"cisco.ios": "5.0.0"})

    def _fake(coll, version=None):
        f = probe._facts(coll, "5.0.0", "local", "deep")
        f["plugins"] = {"cliconf": ["ios"]}
        f["modules"] = ["ios_command"]
        return f
    monkeypatch.setattr(probe, "deep_probe", _fake)
    return onboard.build_onboard_plan("cisco.ios", "cisco_ios", "core_switch", "192.0.2.10",
                                      host_name="sw1", secrets="network", **kw)


def test_key_onboard_emits_path_and_domain_never_the_pem(monkeypatch):
    """A key-auth onboard emits `ansible_ssh_private_key_file` (a kontroll_device_key_dir path), the NON-secret
    `kontroll_ssh_key_domain` hostvar (the render play keys on it to materialize the key), and a
    `<host>_ssh_private_key` SOPS field — but the PEM VALUE is NEVER in the inventory drop-in text (it lives only in
    creds_to_set, destined for SOPS). Guards the key leaking into a git-tracked inventory file (C1)."""
    plan = _plan(monkeypatch, ssh_private_key=_PEM)
    hv = plan["host_block"]["core_switch"]["hosts"]["sw1"]
    assert hv["ansible_ssh_private_key_file"] == "{{ kontroll_device_key_dir }}/sw1.key"
    assert hv["kontroll_ssh_key_domain"] == "network"
    assert plan["creds_to_set"]["sw1_ssh_private_key"] == _PEM + "\n"     # stored with a trailing newline
    assert "BEGIN OPENSSH" not in plan["host_text"]                       # the PEM is NEVER in the drop-in YAML


def test_password_only_onboard_has_no_key_hostvars(monkeypatch):
    """BACKWARD-COMPAT golden: a password-only onboard's drop-in carries NO key hostvars — the key branch is purely
    ADDITIVE, so a host passing no key is structurally unchanged from before the seam. Guards a silent inventory
    drift that would break every existing host's auth on the next fleet run (the one guard the cut refuses to defer)."""
    plan = _plan(monkeypatch, username="admin", password="pw")
    hv = plan["host_block"]["core_switch"]["hosts"]["sw1"]
    assert "ansible_ssh_private_key_file" not in hv and "kontroll_ssh_key_domain" not in hv
    assert hv["ansible_user"].endswith("sw1_username }}") and "ansible_password" in hv   # password path untouched


def test_crlf_paste_is_normalized_with_trailing_newline(monkeypatch):
    """MF-7: a Windows textarea paste (CRLF) is normalized to LF + a trailing newline is ensured before the
    SOPS-store, so paramiko (netcommon_cli) accepts the key and SSH doesn't reject a newline-stripped key. Guards
    a CRLF/short key silently failing the connection."""
    plan = _plan(monkeypatch, ssh_private_key="-----BEGIN-----\r\nLINE\r\n-----END-----")
    stored = plan["creds_to_set"]["sw1_ssh_private_key"]
    assert "\r" not in stored and stored.endswith("\n") and stored == "-----BEGIN-----\nLINE\n-----END-----\n"


def test_apply_routes_creds_through_stdin_never_argv(monkeypatch):
    """MF-3: apply_onboard_plan routes creds (incl. the multi-line PEM) through gitio.sops_write_domain (which
    transits sops' STDIN), NEVER gitio.sops_set (which puts the value on a ps-/proc-visible argv). Pins the
    decrypt->merge->write so a second host's onboard never drops the first's fields. The single most
    security-load-bearing test of this seam. Every WRITER apply_onboard_plan reaches is stubbed — including the
    inventory drop-in writer (gitio.write_inventory_host): with only _write_new stubbed, this test wrote a real
    `instance/inventory/onboarded-cisco_ios.yml` (host `sw1`, TEST-NET) into the checkout on every run — the
    "fossil" the 2026-10-08 review flagged as an operator artefact (S03). It was this test."""
    plan = _plan(monkeypatch, ssh_private_key=_PEM)
    monkeypatch.setattr(gitio, "_write_new", lambda *a, **k: True)
    monkeypatch.setattr(gitio, "write_inventory_host", lambda *a, **k: True)
    monkeypatch.setattr(onboard, "enable_in_fleet", lambda key: False)
    monkeypatch.setattr(gitio, "sops_decrypt_domain", lambda d: {"existing_field": "keep-me"})
    captured = {}
    monkeypatch.setattr(gitio, "sops_write_domain",
                        lambda d, m: captured.update(domain=d, map=dict(m)) or True)
    set_calls = []
    monkeypatch.setattr(gitio, "sops_set", lambda *a, **k: set_calls.append(a) or True)
    onboard.apply_onboard_plan(plan)
    assert not set_calls, "the PEM (and all creds) must go via sops_write_domain stdin, NEVER sops_set argv"
    assert captured["map"]["sw1_ssh_private_key"] == _PEM + "\n"          # the PEM transited the stdin write
    assert captured["map"]["existing_field"] == "keep-me"                # merged: a prior host's field survives


def test_render_play_keeps_no_log_on_the_key():
    """MF-2: the _render-ssh-keys.yml tasks that touch the device key (the decrypt probe + the materializing copy)
    MUST carry `no_log: true` — without it a `--check --diff` or the Semaphore job log (Vector-tailed into Loki)
    would print the PEM. The one line the whole no-leak story rests on; static-asserted like test_file_tail_ssh.py."""
    src = open(os.path.join(ROOT, "ansible", "playbooks", "_render-ssh-keys.yml"), encoding="utf-8").read()
    assert src.count("no_log: true") >= 2, "the decrypt-probe set_fact AND the key-copy task must both be no_log"
    copy_idx = src.index('.key"')                                        # the copy task's dest path
    assert "no_log: true" in src[copy_idx:copy_idx + 500], "the key-materializing copy task must carry no_log: true"


def test_loud_fail_when_a_key_host_is_undecryptable():
    """MF-1 (the blocker's unconditional half): a key-auth host whose SOPS domain is not decryptable in the runner
    context (e.g. `compute` is control-node-only — option B) FAILS LOUDLY via an assert, never silently leaves no
    key (which would surface as a misleading 'Permission denied (publickey)'). Pins the loud-fail can't be dropped."""
    src = open(os.path.join(ROOT, "ansible", "playbooks", "_render-ssh-keys.yml"), encoding="utf-8").read()
    assert "ansible.builtin.assert" in src and "could NOT be decrypted" in src
    assert "_ssh_key_present | bool" in src, "the loud-fail asserts the key was actually decryptable"


def test_render_and_cleanup_force_local_delegation():
    """REGRESSION GUARD (scratch-VM dogfood, 2026-06-18): every materialization/cleanup task MUST carry
    `delegate_to: localhost`. A fleet host's `ansible_connection` (network_cli/ssh/httpapi, set by its
    backend+group_vars) OVERRIDES a play's `connection: local` keyword (Ansible var-beats-keyword precedence), so
    without delegation these LOCAL file ops route over the DEVICE connection — a switch can't run `file`/`copy`, and
    the key NEVER materializes for ANY real host (the dogfood caught exactly this: UNREACHABLE on the dir-create
    task). Pin the count so a newly-added render task that forgets to delegate fails CI, not the next live run."""
    render = open(os.path.join(ROOT, "ansible", "playbooks", "_render-ssh-keys.yml"), encoding="utf-8").read()
    cleanup = open(os.path.join(ROOT, "ansible", "playbooks", "_cleanup-ssh-keys.yml"), encoding="utf-8").read()
    # 5 render tasks (2 asserts + file + set_fact + copy) each delegate; the comment header also names it, so >=5.
    assert render.count("delegate_to: localhost") >= 5, "every render task must delegate_to: localhost"
    assert "delegate_to: localhost" in cleanup, "the cleanup rm must delegate_to: localhost (run_once host hijack)"


def _api_plan(monkeypatch, **kw):
    """Build a real onboard plan for an httpapi (REST) device → the `api` backend (no lab, no install)."""
    monkeypatch.setattr(catalog, "local_installed", lambda: {"fortinet.fortios": "2.0.0"})

    def _fake(coll, version=None):
        f = probe._facts(coll, "2.0.0", "local", "deep")
        f["plugins"] = {"httpapi": ["fortios"]}        # httpapi-only → classifies to the api backend
        f["modules"] = ["fortios_configuration_fact"]
        return f
    monkeypatch.setattr(probe, "deep_probe", _fake)
    return onboard.build_onboard_plan("fortinet.fortios", "fortigate", "edge_firewall", "192.0.2.1",
                                      host_name="fw1", secrets="network", **kw)


def test_onboard_plan_exposes_derived_cred_fields_without_values(monkeypatch):
    """F1: a cliconf onboard's plan carries `cred_fields` (the DERIVED descriptors for THIS backend — SSH login,
    not a token) and `cred_source`, and NO descriptor holds a value. Guards the form being able to render the right
    fields from the plan while the no-leak contract (names-only on every surface) is preserved."""
    plan = _plan(monkeypatch, username="admin", password="pw")
    names = {cf["field"] for cf in plan["cred_fields"]}
    assert {"username", "password", "ssh_private_key"} <= names and "api_token" not in names
    assert plan["cred_source"] == "shallow"
    for cf in plan["cred_fields"]:
        assert "value" not in cf and "secret_value" not in cf      # descriptors are pure schema


def test_api_device_onboard_offers_a_token_not_a_dead_ssh_box(monkeypatch):
    """THE HEADLINE WIN through the planner: onboarding a REST/httpapi device derives an `api_token` field and NO
    `ssh_private_key` — the form stops showing dead SSH boxes for a REST device (the operator's exact gap)."""
    plan = _api_plan(monkeypatch)
    assert plan["error"] is None and plan["backend"] == "api"
    names = {cf["field"] for cf in plan["cred_fields"]}
    assert "api_token" in names and "ssh_private_key" not in names


def test_domain_confinement_refuses_a_cross_domain_secret(monkeypatch):
    """MF-S2: if a derived SECRET field is stamped for a DIFFERENT SOPS domain than the onboard targets, the planner
    RAISES a catchable ValueError BEFORE any cred is mapped — so a malformed/adversarial descriptor can never smuggle
    a secret into a domain the operator didn't choose. The new data-driven loop's load-bearing safety rail (today's
    hardcoded if-ladder couldn't cross domains; a data-driven one could)."""
    monkeypatch.setattr(authspec, "derive_auth", lambda bdef, domain, **k: {
        "source": "shallow",
        "fields": [{"field": "evil", "kind": "password", "secret": True, "sops_stem": "evil",
                    "maps_to": "ansible_password", "domain": "OTHER_DOMAIN"}]})
    with pytest.raises(ValueError, match="refusing to cross domains"):
        _plan(monkeypatch, creds={"evil": "leak-me"})           # onboard targets 'network', field claims 'OTHER_DOMAIN'


def test_render_copy_guarantees_one_trailing_newline():
    r"""REGRESSION GUARD (scratch-VM dogfood, 2026-06-18): the key-materializing copy MUST use a literal block
    (`content: |`) and `| trim`. The community.sops lookup strips the decrypted file's trailing newline, which —
    given yaml.safe_dump's encoding of a value ending in '\n' — drops the KEY's OWN trailing newline on `from_yaml`
    reparse; OpenSSH then rejects the 1-byte-short key ('error in libcrypto' -> publickey denied). `| trim` plus the
    literal block re-add exactly one newline. With the original `content: >-` the dogfood's key-auth SSH FAILED
    (byte-verified: materialized key was 398 bytes / 6 lines vs the valid 399 / 7)."""
    src = open(os.path.join(ROOT, "ansible", "playbooks", "_render-ssh-keys.yml"), encoding="utf-8").read()
    assert "content: |\n" in src, "the key copy must use a literal block (content: |) so a trailing newline survives"
    assert "| trim }}" in src, "the extracted key must be `| trim`-normalized so exactly one newline is re-added"
    assert "content: >-" not in src, "content: >- strips the trailing newline -> OpenSSH 'error in libcrypto'"


def test_a_dotted_host_name_gets_a_lookup_that_resolves_one_key(monkeypatch):
    """The host var that reads a credential out of the decrypted SOPS domain names ONE key. For a host named
    `sw.lab.example` (a name the boundary accepts on purpose) the key is `sw.lab.example_password`, and written as
    `(…).sw.lab.example_password` it is an attribute chain that can never resolve — so that name gets the bracket
    form, while an identifier-shaped name keeps the dotted form byte for byte. Guards both halves: a host whose
    credentials silently cannot be read, and a rewrite of every existing drop-in (a second onboard must still
    report no change)."""
    monkeypatch.setattr(catalog, "local_installed", lambda: {"cisco.ios": "5.0.0"})

    def _fake(coll, version=None):
        f = probe._facts(coll, "5.0.0", "local", "deep")
        f["plugins"] = {"cliconf": ["ios"]}
        f["modules"] = ["ios_command"]
        return f
    monkeypatch.setattr(probe, "deep_probe", _fake)

    def host_vars(host_name):
        plan = onboard.build_onboard_plan("cisco.ios", "cisco_ios", "core_switch", "192.0.2.10",
                                          host_name=host_name, secrets="network", username="admin", password="pw")
        return plan["host_block"]["core_switch"]["hosts"][host_name], plan["creds_to_set"]

    plain, _ = host_vars("sw-core-1")
    assert plain["ansible_password"].endswith(").sw_core_1_password }}")          # unchanged: the dotted form
    dotted, creds = host_vars("sw.lab.example")
    assert dotted["ansible_password"].endswith(")['sw.lab.example_password'] }}")   # one key, in brackets
    assert "sw.lab.example_password" in creds
    assert onboard._sops_ref("X", "a_b") == "{{ (X).a_b }}" and onboard._sops_ref("X", "9a") == "{{ (X)['9a'] }}"


@pytest.mark.parametrize("stem", ["pw }} {{ lookup('pipe', 'id')", "pw']", "pw.x", "pw-x", "", "pw\n", None])
def test_a_credential_field_whose_name_is_not_an_identifier_is_refused(monkeypatch, stem):
    """A credential field's stem is formatted into the host var's lookup expression, so it is an identifier or the
    onboard is refused before anything is rendered — as REPOSITORY DATA (nothing the caller sent is wrong: the
    backend registry or the class's module file declared the name; an installed module's option with such a name
    is not offered as a field at all, tests/unit/test_authspec.py), saying where to look and not echoing the name.
    Guards a declared field called `pw }} {{ … }}` becoming a Jinja expression in a host's inventory variables the
    moment an operator fills that field in — and the refusal being answered as a field the operator got wrong."""
    monkeypatch.setattr(authspec, "derive_auth", lambda *a, **k: {"fields": [
        {"field": "password", "kind": "password", "label": "password", "maps_to": "ansible_password",
         "sops_stem": stem, "secret": True, "required": False}]})
    from kontroll import paths
    with pytest.raises(paths.RepositoryDataError) as e:
        _plan(monkeypatch, password="pw")
    assert e.value.what == "credential field" and "lookup" not in str(e.value)
    assert "device class" in str(e.value) and "registry entry" in str(e.value)      # where to look
    assert not isinstance(e.value, ValueError)


def test_an_inline_credential_field_reaches_the_inventory_only_as_a_plain_word(monkeypatch):
    """For a credential field marked `inline: true` the PLANNER writes the value itself into the host's inventory
    variables — and refuses a value that is not one plain word, with the field named, while `8443` lands unchanged.
    The helper is pinned on its own elsewhere; this pins the CALL SITE, because no shipped backend declares an
    inline field and nothing else would notice the check being bypassed. Guards `{{ … }}` in a literal host var,
    which Ansible evaluates on the runner when a play touches the host."""
    from kontroll import paths
    monkeypatch.setattr(authspec, "derive_auth", lambda *a, **k: {"fields": [
        {"field": "api_port", "kind": "port", "label": "port", "maps_to": "ansible_httpapi_port",
         "sops_stem": "api_port", "inline": True, "secret": False, "required": False}], "source": "shallow"})
    with pytest.raises(paths.RequestBoundaryError) as e:
        _plan(monkeypatch, creds={"api_port": "{{ 7*7 }}"})
    assert e.value.field == "api_port"
    plan = _plan(monkeypatch, creds={"api_port": "8443"})
    assert plan["host_block"]["core_switch"]["hosts"]["sw1"]["ansible_httpapi_port"] == "8443"
    assert "api_port" not in str(plan["creds_to_set"])                   # an inline value is not a SOPS secret
