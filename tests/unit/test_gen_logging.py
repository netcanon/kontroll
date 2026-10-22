"""scripts/gen-logging.py — the modules x inventory -> Vector drop-in generator (the capability-derived half).
Guards the JOIN fanning a real `logs:` block, the --check staleness contract (generated-never-hand-maintained),
and the fail-closed config-injection + C12 label-hygiene guards (a bad param or a secret-bearing label exits
non-zero, never reaching the generated config).
"""
import importlib.util
import os
import shutil
import subprocess

import pytest
import yaml

from kontroll import paths

pytestmark = pytest.mark.unit

# The committed PUBLIC reference inventory. The committed docker/vector/generated/*.yaml derive from THIS (never
# the private `instance/` overlay), so the SHIPPED tree carries only TEST-NET addrs — no real homelab topology
# (the F3 leak fix). The maintainer's live deploy still regenerates from their private overlay.
EXAMPLE = os.path.join(paths.ROOT, "instance.example")
_EX_FLEET = os.path.join(EXAMPLE, "fleet.yml")
_EX_INV = os.path.join(EXAMPLE, "inventory", "hosts.yml")


def _gen():
    spec = importlib.util.spec_from_file_location(
        "gen_logging", os.path.join(paths.ROOT, "scripts", "gen-logging.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_example_fleet_fans_the_cisco_ios_syslog_source():
    """The example fleet's cisco_ios `logs: [{method: syslog_push}]` renders a Vector syslog SOURCE + a shaping
    transform + feeds the aggregate capability sink — guards the JOIN (enabled module x logs entry x hosts)
    going silently empty (the logging analogue of the telemetry target fan-out). Pinned against the PUBLIC
    example inventory (TEST-NET addrs — F3)."""
    gen = _gen()
    files = gen.logging_files(gen._load(_EX_FLEET), gen._load(_EX_INV))
    src = "docker/vector/generated/syslog_push_cisco_ios.generated.yaml"
    assert src in files and "type: syslog" in files[src]
    assert any(p.endswith("_capability_sink.generated.yaml") for p in files)


def test_committed_drop_ins_match_the_example_inventory():
    """The committed docker/vector/generated/*.yaml == what gen-logging produces from the PUBLIC example
    inventory (instance.example/) — the staleness gate keyed off the PUBLIC source, so the SHIPPED tree carries
    only TEST-NET addrs and a real-valued regeneration committed by mistake fails HERE (the F3 leak-regression
    gate; supersedes the old overlay-driven `--check` that read the private overlay and leaked real homelab IPs
    into the committed config). A `logs:` block edit not regenerated against the example also fails here.
    (`scripts/gen-logging.py --check` stays the maintainer's OVERLAY-driven CLI for live deploy.)"""
    gen = _gen()
    for rel, body in gen.logging_files(gen._load(_EX_FLEET), gen._load(_EX_INV)).items():
        path = os.path.join(paths.ROOT, rel)
        assert os.path.exists(path), "missing committed drop-in %s — re-run gen-logging against instance.example/" % rel
        assert open(path, encoding="utf-8").read() == body, \
            "%s drifted from the example inventory — re-run gen-logging against instance.example/" % rel


def test_param_outside_allowlist_is_rejected():
    """A logs: param value not in the method's closed allow-list — including a metacharacter-bearing one — is
    REJECTED at generate time (sys.exit), never interpolated into the Vector config (the config-injection
    guard, C12; mirror of the snmp param-rejection test)."""
    gen = _gen()
    method = {"name": "syslog_push", "params": {"transport": {"allowed": ["tcp", "udp"], "required": False}}}
    with pytest.raises(SystemExit):
        gen._validate_params_or_exit(method, {"transport": "rsh; rm -rf /"}, "cisco_ios")


def test_non_canonical_label_is_rejected():
    """A method emitting a label outside the canonical NON-SECRET set exits non-zero at generate time — so a
    credential-bearing or high-cardinality label can never reach Loki's unencrypted index (C12 label hygiene,
    the security pin V1 bolded; the fresh _validate_labels_or_exit gate)."""
    gen = _gen()
    with pytest.raises(SystemExit):
        gen._validate_labels_or_exit({"name": "bad", "labels": ["host", "auth_token"]}, "demo")


def test_transform_vrl_is_spliced_before_the_canonical_defaults():
    """A method's `transform_vrl` (the data-driven field derivation) renders into the shaping transform AHEAD
    of the `.service`/`.host`/`.level` defaults (S9) — so a real derived value wins and the default is only the
    fallback. Guards the splice order silently inverting (a default clobbering the derivation) or the block
    being dropped on the floor."""
    gen = _gen()
    method = {"name": "syslog_push", "kind": "syslog", "labels": ["host", "service", "level"],
              "transform_vrl": ['if exists(.severity) { .level = string(.severity) }']}
    _tid, frag = gen._render_fragment(method, "cisco_ios", "g", {"sw1": "198.51.100.1"})
    body = frag["transforms"]["syslog_push_cisco_ios_shape"]["source"]
    assert "if exists(.severity)" in body, "a transform_vrl line must render into the shaping transform"
    assert body.index(".severity") < body.index("if !is_string(.service)"), \
        "transform_vrl must splice BEFORE the canonical .service default (derivation wins; the default is fallback)"


def test_canonical_defaults_are_the_infallible_guard_form_not_a_coalesce():
    """The canonical .service/.host/.level FALLBACKS render as an infallible `if !is_string(x) { x = … }` guard,
    NEVER as `string(x) ?? "default"`. Why (dogfood-caught on proxmox_api): when a method's transform_vrl PROVABLY
    assigns a string to the field (e.g. `.level = if … {"info"} else {"error"}`), the VRL compiler proves
    `string(.level)` can't fail and Vector REJECTS the now-unnecessary `??` as E651 — Vector exits config-error
    (code 78) and the whole pipeline is down. The guard form is identical in effect but never coalesces, so it
    compiles whether the spliced derivation leaves the field provably-string or maybe-null. A regression here =
    Vector won't start. Rendered with a transform_vrl that provably-assigns .level, exactly like proxmox_api."""
    gen = _gen()
    method = {"name": "demo_pull", "kind": "http_client", "labels": ["host", "service", "level"],
              "transform_vrl": ['.level = if .ok == true { "info" } else { "error" }']}
    _tid, frag = gen._render_fragment(method, "k", "g", {"h1": "198.51.100.1"})
    body = frag["transforms"]["demo_pull_k_shape"]["source"]
    assert "?? " not in body, "no `?? default` coalesce in the baseline — a provably-string field makes it E651"
    for fld in ("service", "host", "level"):
        assert "if !is_string(.%s) {" % fld in body, "the .%s default must be the infallible is_string guard" % fld


def test_baseline_shaping_is_generic_no_method_name_branch():
    """A method with NO `transform_vrl` renders a shaping transform carrying ZERO method-specific field names —
    no `.severity`, no `._SYSTEMD_UNIT` — only the generic structural labels + canonical defaults. Proves
    gen-logging does NOT branch on the method NAME (no `if method == "syslog"`): the severity/unit derivation
    lives in the methods' transform_vrl DATA, not the generator. Rendered for a method literally named
    'syslog_push' with its transform_vrl stripped — a lurking name-branch would re-inject `.severity` here."""
    gen = _gen()
    method = {"name": "syslog_push", "kind": "syslog", "labels": ["host", "service", "level"]}  # no transform_vrl
    _tid, frag = gen._render_fragment(method, "cisco_ios", "g", {"sw1": "198.51.100.1"})
    body = frag["transforms"]["syslog_push_cisco_ios_shape"]["source"]
    assert ".severity" not in body and "_SYSTEMD_UNIT" not in body, \
        "the generic baseline must carry NO method-specific field — derivation is transform_vrl data only"


def test_journald_unit_and_priority_derivation_renders_from_the_real_descriptor():
    """The committed journald_remote.yml's transform_vrl maps `._SYSTEMD_UNIT` -> .service and the numeric
    PRIORITY -> a canonical level via to_syslog_level — the SAME derivation the live internal journald transform
    uses. Loaded through catalog.load_logging (the real descriptor), it must render verbatim into the shaping
    transform for a journald class. Guards the journald derivation silently collapsing to the
    method-name/'info' fallbacks (which would lose the real unit + level on every journald event)."""
    gen = _gen()
    jr = {m["name"]: m for m in gen.catalog.load_logging()}["journald_remote"]
    _tid, frag = gen._render_fragment(jr, "host_node", "g", {"h1": "198.51.100.2"})
    body = frag["transforms"]["journald_remote_host_node_shape"]["source"]
    assert "string(._HOSTNAME)" in body, "journald transform_vrl must map the uploading host's ._HOSTNAME to .host"
    assert "string(._SYSTEMD_UNIT)" in body, "journald transform_vrl must map the systemd unit to .service"
    assert "to_syslog_level(to_int(.PRIORITY)" in body, "journald transform_vrl must derive .level from PRIORITY"


def test_journald_remote_is_an_exec_journalctl_source_not_http_server_or_journald():
    """journald_remote reads the systemd-journal-remote output dir via an `exec` journalctl source — NOT an
    http_server (Vector can't decode the binary journal export format systemd-journal-upload streams) and NOT a
    Vector `journald` source (it filters to the current boot +0 and DROPS remote journals whose boot differs).
    BOTH were LIVE-DISPROVEN on the bench and reverted (PR #12/#13); the exec path is LIVE-PROVEN end-to-end to
    Loki (local/s10-journald-receiver-bench-findings.md). This pin CODIFIES that hard-won result so neither broken
    mechanism can silently return: source must be `exec`, command `journalctl --directory=... -o json -f`, decode
    json. A regression here = a receiver that ingests 0 events again (or drops every remote boot)."""
    gen = _gen()
    jr = {m["name"]: m for m in gen.catalog.load_logging()}["journald_remote"]
    assert jr["kind"] == "exec", "journald_remote must be an exec source (http_server/journald were live-disproven)"
    cmd = jr["source"]["command"]
    assert cmd[0] == "journalctl" and "-o" in cmd and "json" in cmd and "-f" in cmd, \
        "the exec command must be `journalctl ... -o json -f` (clean NDJSON, all boots), got %r" % cmd
    assert any(a.startswith("--directory=") for a in cmd), "journalctl must read the remote dir via --directory="
    assert jr["source"]["decoding"]["codec"] == "json", "the exec source must decode the journalctl NDJSON as json"
    assert "current_boot_only" not in jr["source"], "exec journalctl has no current_boot_only (the journald-source trap)"


def test_file_tail_ssh_is_an_exec_ssh_pull_with_a_per_host_target():
    """file_tail_ssh (S11) PULLs a remote host's journald over SSH: an `exec` source whose command is
    `ssh ... root@${DEVICE_ADDR}` — the host FORCES `journalctl -f -o json` server-side via the read key, so the
    client sends ssh only — decoding the resulting NDJSON. NOT the old kind:file local tail (no module ever used
    it; the operator chose the journald-over-SSH model). Pins the exec-ssh shape + the credential model so a
    regression can't silently revert it to a file source, drop the `-i` key, or lose the json decoding."""
    gen = _gen()
    m = {x["name"]: x for x in gen.catalog.load_logging()}["file_tail_ssh"]
    assert m["kind"] == "exec" and m["direction"] == "pull", "file_tail_ssh must be an exec PULL (ssh reaches out)"
    cmd = m["source"]["command"]
    assert cmd[0] == "ssh", "the exec command must invoke ssh (PULL over SSH), got %r" % cmd
    assert "-i" in cmd, "ssh must use an explicit -i identity (the mounted read key)"
    assert any("${DEVICE_ADDR}" in a for a in cmd), "the ssh target must template ${DEVICE_ADDR} (per-host pull)"
    assert m["source"]["decoding"]["codec"] == "json", "decode the forced `journalctl -o json` NDJSON"
    assert m["secret_domain"] == "logging_file_tail", "the read keypair is the logging_file_tail SOPS domain"
    assert "secret_env_map" not in m, "the key is a FILE (mounted RO), never an env token -> no secret_env_map"


def test_subst_recurses_into_exec_command_lists_for_per_host_pull():
    """gen-logging's _subst substitutes ${DEVICE_ADDR} element-wise inside a LIST (an exec source's command argv)
    AND inside a dict, so file_tail_ssh's `ssh ... root@${DEVICE_ADDR}` reaches the declaring host. Before the
    recursion fix _subst returned a list unchanged (only scalars were substituted), so the per-host pull target
    would have stayed a literal ${DEVICE_ADDR} — ssh to a bogus host. A placeholder-free list (journald_remote's
    static journalctl command) must be returned byte-unchanged. Also verifies the end-to-end render lands the
    declaring host's address in the generated source command."""
    gen = _gen()
    out = gen._subst(["ssh", "root@${DEVICE_ADDR}", {"k": "x-${DEVICE_ADDR}"}], "0.0.0.0", "198.51.100.87")
    assert out == ["ssh", "root@198.51.100.87", {"k": "x-198.51.100.87"}], "list/dict elements must be substituted"
    assert gen._subst(["journalctl", "-f"], "0.0.0.0", "198.51.100.87") == ["journalctl", "-f"], \
        "a placeholder-free list (journald_remote's static command) must be returned unchanged"
    m = {x["name"]: x for x in gen.catalog.load_logging()}["file_tail_ssh"]
    _tid, frag = gen._render_fragment(m, "host_node", "g", {"h1": "198.51.100.5"})
    cmd = frag["sources"]["file_tail_ssh_host_node_src"]["command"]
    assert "root@198.51.100.5" in cmd, "the rendered exec command must target the declaring host's addr, got %r" % cmd
    assert "root@${DEVICE_ADDR}" not in cmd, "the ${DEVICE_ADDR} placeholder must be substituted, not left literal"


def test_proxmox_api_pull_renders_env_only_custom_auth():
    """The proxmox_api method renders the PVE token as a `${ENV}` reference in the http_client `custom` auth
    header — EXACTLY `PVEAPIToken=${KONTROLL_PVE_LOG_TOKEN}`, never an inline literal. Guards a credential
    leaking into the generated (committed) Vector config (C12): the token value lives ONLY in the
    deploy-rendered .env on the live host, interpolated by Vector at runtime. Pins the credentialed-pull seam."""
    gen = _gen()
    files = gen.logging_files(gen._load(_EX_FLEET), gen._load(_EX_INV))
    f = "docker/vector/generated/proxmox_api_proxmox.generated.yaml"
    assert f in files, "proxmox declares logs: [proxmox_api] -> a generated http_client pull fragment"
    assert "value: PVEAPIToken=${KONTROLL_PVE_LOG_TOKEN}" in files[f], \
        "the PVE token must be an ${ENV} ref, never an inline secret in the committed config (C12)"
    assert "strategy: custom" in files[f], "Proxmox needs a custom Authorization header (PVEAPIToken, not Bearer)"


def test_proxmox_api_tls_is_derived_from_the_class_vendor_default_not_hardcoded():
    """proxmox_api carries NO literal `tls:` in its descriptor (the no-bespoke-config tenet, B2) — gen-logging
    INJECTS `source.tls` from the proxmox class `vendor_defaults.tls_posture` (self_signed → verify_certificate
    false) via the `tls_from_class` flag + kontroll.endpoints. Guards a regression that re-hardcodes the posture
    in the descriptor, AND proves the derived value equals the historical literal (so the generated config is
    unchanged: derive, don't drift)."""
    gen = _gen()
    desc = gen._load("logging/proxmox_api.yml")
    assert "tls" not in (desc.get("source") or {}), "proxmox_api must NOT hardcode source.tls — derive it from the class"
    assert desc.get("tls_from_class") is True, "proxmox_api must opt into class-derived TLS (tls_from_class)"
    m = {x["name"]: x for x in gen.catalog.load_logging()}["proxmox_api"]
    _tid, frag = gen._render_fragment(m, "proxmox", "g", {"pve": "198.51.100.2"},
                                      vendor_defaults={"tls_posture": "self_signed"})
    assert frag["sources"]["proxmox_api_proxmox_src"]["tls"] == {"verify_certificate": False}, \
        "self_signed class posture must render verify_certificate:false (== the retired hardcode)"


def test_class_tls_ca_pin_flips_verify_on_with_the_mounted_bundle_path():
    """An instance CA pin (device_trust.<key>.tls_ca_file = the operator's HOST path) flips the rendered
    `source.tls` to verify ON and points ca_file at the MOUNTED in-container bundle (endpoints.VECTOR_CA_BUNDLE) —
    NOT the operator's raw host path (a bind needs host-source + container-target; deploy-stack assembles the
    bundle + vector.yaml mounts it). Guards the instance-decision seam being ignored AND the host-path-vs-
    container-path bug (writing the operator's disk path verbatim, which Vector couldn't open)."""
    gen = _gen()
    m = {x["name"]: x for x in gen.catalog.load_logging()}["proxmox_api"]
    _tid, frag = gen._render_fragment(m, "proxmox", "g", {"pve": "198.51.100.2"},
                                      vendor_defaults={"tls_posture": "self_signed"},
                                      instance_trust={"tls_ca_file": "instance/certs/pve-ca.pem"})
    assert frag["sources"]["proxmox_api_proxmox_src"]["tls"] == \
        {"verify_certificate": True, "ca_file": gen.endpoints.VECTOR_CA_BUNDLE}, \
        "ca_file must be the MOUNTED container bundle path, never the operator's raw host path"


def test_tls_from_class_with_no_class_posture_fails_closed():
    """A `tls_from_class` method whose class declares no `vendor_defaults` EXITS non-zero at generate time
    (fail-closed via kontroll.endpoints → sys.exit) — never silently verify=False. Guards a class that opts into
    class-TLS but forgets to declare the posture (the unsafe-default trap)."""
    gen = _gen()
    m = {"name": "x", "kind": "http_client", "labels": ["host"],
         "source": {"endpoint": "https://${DEVICE_ADDR}/y"}, "tls_from_class": True}
    with pytest.raises(SystemExit):
        gen._render_fragment(m, "k", "g", {"h": "198.51.100.1"})        # vendor_defaults=None → resolver raises → exit


def test_pull_endpoint_address_and_host_label_name_the_same_host():
    """For a PULL method, gen-logging derives the endpoint address and the `.host` label from ONE sorted
    (name, addr) pair — they MUST name the same host. Guards the bug the first real pull source exposed: two
    independent sorts (by addr for the endpoint, by name for the label) picked DIFFERENT hosts when the
    orderings disagreed (endpoint sorted by-addr but label sorted by-name), mislabeling the stream."""
    gen = _gen()
    method = {"name": "rest_pull", "kind": "http_client", "labels": ["host", "service"],
              "source": {"endpoint": "https://${DEVICE_ADDR}/x"}}
    # name-order: "alpha" < "zeta"; addr-order: .9 (zeta) < .10 (alpha) -> the two sorts DISAGREE.
    hosts = {"zeta": "198.51.100.9", "alpha": "198.51.100.10"}
    _tid, frag = gen._render_fragment(method, "k", "g", hosts)
    endpoint = frag["sources"]["rest_pull_k_src"]["endpoint"]
    body = frag["transforms"]["rest_pull_k_shape"]["source"]
    assert 'if !is_string(.host) { .host = "alpha" }' in body, "label must be the sorted-by-NAME host"
    assert "198.51.100.10" in endpoint and "198.51.100.9" not in endpoint, \
        "endpoint must be that SAME host's addr (.10), not the sorted-by-addr host (.9)"


def test_pull_unnest_and_dedupe_render_a_threaded_chain():
    """A pull method declaring `pull_unnest` + `dedupe_key` renders src -> unnest -> dedupe -> shape with the
    `inputs:` threaded so each stage consumes the previous (the shape consumes the DEDUPE, not the source). The
    data-driven pull-fan enhancement: one deduped event per array element, NO method-name branch. Guards a
    mis-wired chain (a stage consuming the wrong input) silently dropping the fan/dedupe."""
    gen = _gen()
    method = {"name": "rest_pull", "kind": "http_client", "direction": "pull", "labels": ["host", "service"],
              "source": {"endpoint": "https://${DEVICE_ADDR}/x"}, "pull_unnest": "data", "dedupe_key": "data.id"}
    _tid, frag = gen._render_fragment(method, "k", "g", {"h1": "198.51.100.1"})
    t = frag["transforms"]
    assert t["rest_pull_k_unnest"]["inputs"] == ["rest_pull_k_src"], "unnest consumes the source"
    assert "unnest!(.data)" in t["rest_pull_k_unnest"]["source"], \
        "unnest must be the abort-on-error form INSIDE the is_array guard — a plain unnest(.x) is a fallible " \
        "assignment (VRL E103) because is_array does not narrow the field's type (dogfood-caught)"
    assert "is_array(.data)" in t["rest_pull_k_unnest"]["source"], "the unnest! must be is_array-guarded"
    assert t["rest_pull_k_dedupe"]["type"] == "dedupe" and t["rest_pull_k_dedupe"]["inputs"] == ["rest_pull_k_unnest"]
    assert t["rest_pull_k_dedupe"]["fields"]["match"] == ["data.id"], "dedupe keys on the declared field path"
    assert t["rest_pull_k_shape"]["inputs"] == ["rest_pull_k_dedupe"], "shape must consume the LAST present stage"


def test_a_method_without_fan_fields_renders_only_the_shape_transform():
    """A method that declares NEITHER pull_unnest NOR dedupe_key renders exactly source + ONE shape transform —
    the optional stages are byte-absent, so every existing method (syslog/journald/the batch proxmox) is
    unchanged. Guards the optional-stage rendering ever leaking an empty unnest/dedupe into an unrelated method."""
    gen = _gen()
    method = {"name": "rest_pull", "kind": "http_client", "direction": "pull", "labels": ["host", "service"],
              "source": {"endpoint": "https://${DEVICE_ADDR}/x"}}
    _tid, frag = gen._render_fragment(method, "k", "g", {"h1": "198.51.100.1"})
    assert set(frag["transforms"].keys()) == {"rest_pull_k_shape"}, "only the shape transform when no fan fields"
    assert frag["transforms"]["rest_pull_k_shape"]["inputs"] == ["rest_pull_k_src"]


def test_pull_fan_fields_are_fail_closed():
    """_validate_pull_shape_or_exit: a metacharacter-bearing pull_unnest (a config-injection attempt) or either
    fan field on a non-pull (push) method exits non-zero at GENERATE time — fail-closed config-as-data (a bad
    field name must never be interpolated into `unnest()`/a dedupe path; a push listener has no array to fan).
    The well-formed and absent cases must NOT exit."""
    gen = _gen()
    with pytest.raises(SystemExit):                                          # metacharacter -> injection attempt
        gen._validate_pull_shape_or_exit({"name": "m", "direction": "pull", "pull_unnest": "data); evil("}, "k")
    with pytest.raises(SystemExit):                                          # a fan field on a push method
        gen._validate_pull_shape_or_exit({"name": "m", "direction": "push", "dedupe_key": "upid"}, "k")
    gen._validate_pull_shape_or_exit(
        {"name": "m", "direction": "pull", "pull_unnest": "data", "dedupe_key": "data.upid"}, "k")   # well-formed
    gen._validate_pull_shape_or_exit({"name": "m"}, "k")                     # absent -> no-op


def test_credentialed_source_env_must_be_declared_in_secret_env_map():
    """A logging source whose `source.auth.value` interpolates `${ENV}` MUST declare that ENV in its
    `secret_env_map` — the single source of truth, so the Vector auth value and the unified secret->env manifest
    (gen-secret-env builds it from secret_env_map) can't drift. Guards a credentialed source shipping an auth header
    deploy-stack never fills: the env var wouldn't be in the manifest -> Vector gets an empty token -> silent auth
    failure. (The manifest EMISSION + secret_env_map SHAPE validation live in gen-secret-env; this is the
    logging-source cross-check that the auth value and the map agree.) A source with no `${ENV}` must NOT exit."""
    gen = _gen()
    with pytest.raises(SystemExit):                                          # references ${ENV} but no secret_env_map
        gen._validate_credentialed_source_or_exit(
            {"name": "m", "source": {"auth": {"strategy": "custom", "value": "X=${KONTROLL_M_TOKEN}"}}}, "k")
    with pytest.raises(SystemExit):                                          # references a DIFFERENT env than declared
        gen._validate_credentialed_source_or_exit(
            {"name": "m", "source": {"auth": {"value": "X=${OTHER}"}},
             "secret_env_map": {"KONTROLL_M_TOKEN": "{a}"}}, "k")
    gen._validate_credentialed_source_or_exit(                              # ${ENV} declared -> OK, no exit
        {"name": "m", "source": {"auth": {"value": "X=${KONTROLL_M_TOKEN}"}},
         "secret_env_map": {"KONTROLL_M_TOKEN": "{a}"}}, "k")
    gen._validate_credentialed_source_or_exit({"name": "m", "source": {}}, "k")   # uncredentialed -> no exit


def test_malformed_transform_vrl_is_rejected():
    """A `transform_vrl` that is a bare string (a `+= list(...)` would splice it char-by-char into garbage VRL)
    or that carries a non-string entry exits non-zero at GENERATE time — the fail-closed config-as-data shape
    guard, so a mistyped log-method descriptor fails before it can emit broken VRL that only a live Vector
    startup would otherwise catch — the gate does not compile a generated fragment (C12). The absent +
    well-formed cases must NOT exit."""
    gen = _gen()
    with pytest.raises(SystemExit):
        gen._validate_transform_vrl_or_exit({"name": "m", "transform_vrl": "not a list"}, "k")
    with pytest.raises(SystemExit):
        gen._validate_transform_vrl_or_exit({"name": "m", "transform_vrl": [".ok = 1", 42]}, "k")
    gen._validate_transform_vrl_or_exit({"name": "m", "transform_vrl": [".level = string(.severity)"]}, "k")
    gen._validate_transform_vrl_or_exit({"name": "m"}, "k")          # absent -> no-op, must not exit


def test_no_logs_block_contributes_no_source():
    """A class with hosts but no `logs:` block emits NO Vector drop-in — the no-block => no-source honesty
    rule, so the generator never invents a log source for an undeclared class. Guards a fan-out that fabricates
    sources for unlogged modules."""
    gen = _gen()
    # openwrt is enabled with hosts but declares no logs: block — it must contribute nothing.
    fleet = {"enabled_modules": ["openwrt"]}
    files = gen.logging_files(fleet, gen._load(_EX_INV))
    assert not any("openwrt" in p for p in files)


_JOURNALD_METHODS = ("journald_remote", "file_tail_ssh")
_INTERNAL_JOURNALD = os.path.join(paths.ROOT, "docker", "vector", "config.d", "transforms",
                                  "internal_journald_shape.yaml")


def _fold_rules(path):
    """The `unit = replace(…)` lines of a file, its comment and a list item's `- ` stripped, in order."""
    with open(path, encoding="utf-8") as fh:
        lines = [ln.split("  #")[0].strip() for ln in fh]
    return [ln[2:] if ln.startswith("- ") else ln for ln in lines if ln.lstrip("- ").startswith("unit = replace(")]


def test_the_journald_methods_fold_the_unit_by_the_internal_transforms_rules():
    """journald_remote and file_tail_ssh fold `.service` by the internal journald transform's rules — what systemd
    mints per login, run, container scope or template instance — line for line, and each rule reaches the rendered
    shaping transform. WHY: one journald_remote receiver serves many hosts, so a rule missing there opens a Loki
    stream per login on every one of them; the rules live in three files, and the fix that first folded the
    internal transform's (2026-10-22) left both methods raw while their comments still claimed parity."""
    internal = _fold_rules(_INTERNAL_JOURNALD)
    assert len(internal) >= 4, internal
    gen = _gen()
    methods = {m["name"]: m for m in gen.catalog.load_logging()}
    for name in _JOURNALD_METHODS:
        assert _fold_rules(os.path.join(paths.ROOT, "logging", name + ".yml")) == internal, name
        _tid, frag = gen._render_fragment(methods[name], "host_node", "g", {"h1": "198.51.100.2"})
        body = frag["transforms"][name + "_host_node_shape"]["source"]
        assert all(rule in body for rule in internal), (name, body)


@pytest.mark.skipif(shutil.which("vector") is None, reason="needs the vector binary (the gate image has it)")
@pytest.mark.parametrize("name", _JOURNALD_METHODS)
def test_a_rendered_journald_method_folds_the_unit_under_vector(tmp_path, name):
    """Each journald method's rendered fragment, run under `vector test`: a login's scope folds to
    `session.scope`, a template's instance is dropped, a plain unit is kept. WHY: nothing else compiles a rendered
    capability fragment's VRL (SECURITY.md C22, known gaps), and the fold reaches it from YAML through the
    generator with its `$` doubled for Vector's env interpolation — a slip there would compile nowhere until a box
    enabled the method."""
    gen = _gen()
    method = {m["name"]: m for m in gen.catalog.load_logging()}[name]
    _tid, frag = gen._render_fragment(method, "host_node", "g", {"h1": "198.51.100.2"})
    shape = name + "_host_node_shape"
    cases = [("session-42.scope", "session.scope"), ("getty@tty1.service", "getty@.service"),
             ("sshd.service", "sshd.service")]
    frag["tests"] = [{
        "name": "%s: %s" % (name, unit),
        "inputs": [{"insert_at": shape, "type": "log",
                    "log_fields": {"message": "x", "PRIORITY": "6", "_SYSTEMD_UNIT": unit}}],
        "outputs": [{"extract_from": shape, "conditions": [
            {"type": "vrl", "source": 'assert_eq!(.service, "%s")' % want}]}],
    } for unit, want in cases]
    cfg = tmp_path / "fragment.yaml"
    cfg.write_text(yaml.safe_dump(frag, sort_keys=False), encoding="utf-8")
    r = subprocess.run(["vector", "test", str(cfg)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, (r.stdout + r.stderr)[-3000:]
