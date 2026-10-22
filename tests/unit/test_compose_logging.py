"""Logging internal-stack compose/config pins (Theme 1: Loki + Vector).

These are the security/at-rest invariants the logging pipeline must not regress. They read the committed
docker fragments + Loki config as data and assert the C12/C3/C8 posture the design locked, so a careless edit
(exposing Loki on all interfaces, a writable host mount, a named volume for the secret-bearing store, or an
empty/infinite retention) fails loudly in CI rather than on a live deploy.
"""
import glob
import http.server
import os
import re
import shutil
import socket
import subprocess
import threading

import pytest
import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The canonical, low-cardinality, NON-SECRET Loki label set (C12). The internal sink omits `device`
# (capability-derived streams add it); every label key must be in this set.
_CANONICAL_LABELS = {"source", "host", "service", "level", "run_id", "device"}


def _load(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_loki_binds_mgmt_ip_never_all_interfaces():
    """docker/services/loki.yaml publishes :3100 on ${KONTROLL_MGMT_IP} only, never 0.0.0.0 or a literal IP.
    Guards the log store's query/push API being exposed on every interface (C3/C12 mgmt-only); a regression
    here would put the secret-adjacent Loki API on the WAN side of a multi-homed control VM."""
    ports = _load("docker/services/loki.yaml")["services"]["loki"]["ports"]
    assert ports, "loki must publish its mgmt-bound API port"
    for p in ports:
        assert "${KONTROLL_MGMT_IP" in p, "loki port %r must bind ${KONTROLL_MGMT_IP}, not %r" % (p, p)
        assert "0.0.0.0" not in p


def test_vector_publishes_only_the_mgmt_bound_syslog_ingress():
    """docker/services/vector.yaml publishes EXACTLY the syslog-push ingress (:5514), bound to
    ${KONTROLL_MGMT_IP} only — never 0.0.0.0, a literal IP, or the unauthenticated Vector API (:8686, which
    stays loopback-internal). Vector is a collector, not a query surface; the syslog ingress is the one host
    port the syslog_push logging method needs, and it must stay mgmt-VLAN-only (C3/C12). Guards a regression
    that exposes the API or binds the ingress on all interfaces. The previous invariant was 'no ports at all';
    it changed when the device-side syslog_push wiring landed (a device must have a mgmt-bound port to ship to)."""
    ports = _load("docker/services/vector.yaml")["services"]["vector"].get("ports") or []
    assert ports, "vector must publish the mgmt-bound syslog ingress for syslog_push"
    for p in ports:
        assert "${KONTROLL_MGMT_IP" in p, "vector port %r must bind ${KONTROLL_MGMT_IP}" % p
        assert "0.0.0.0" not in p
        assert ":5514:" in p, "the only published vector port is the syslog ingress :5514, got %r" % p
        assert "8686" not in p, "the Vector API (8686) must never be published — it stays loopback-internal"


def test_vector_host_log_mounts_are_read_only():
    """Every host-log source mount in vector.yaml (docker.sock, journald, the audit/run-log dirs) is `:ro`.
    Guards a writable mount giving the privileged collector host-write — C12 minimal-privilege (Vector reads,
    never writes a source). The config-tree mount (/etc/vector) and the vector-data buffer are exempt."""
    vol = _load("docker/services/vector.yaml")["services"]["vector"]["volumes"]
    host_mounts = [v for v in vol if v.startswith("/") or v.startswith("${KONTROLL_OPERATOR_HOME")]
    assert host_mounts, "vector must mount the host log sources"
    for m in host_mounts:
        # the named-volume buffer (vector-data:/var/lib/vector) is not a host path; only host paths are checked.
        assert m.endswith(":ro"), "vector host mount must be read-only (:ro): %r" % m


def test_loki_store_is_a_bind_mount_not_a_named_volume():
    """Loki's store is the relocatable BIND MOUNT ${KONTROLL_LOGS_DIR:-/var/lib/kontroll/loki}:/loki (a
    HOST path, fail-closed to today's exact dir), and loki.yaml declares no `loki-data` named volume. Guards
    the secret-bearing log bodies landing under the Docker volume root with default perms instead of the
    operator-owned 0750/uid-10001 crown-jewel dir the C8 at-rest boundary requires (must-fix #11)."""
    loki = _load("docker/services/loki.yaml")
    vols = loki["services"]["loki"]["volumes"]
    assert any(v.startswith("${KONTROLL_LOGS_DIR:-/var/lib/kontroll/loki}:/loki") for v in vols), \
        "loki store must be a fail-closed bind mount ${KONTROLL_LOGS_DIR:-/var/lib/kontroll/loki}"
    assert "volumes" not in loki or "loki-data" not in (loki.get("volumes") or {}), \
        "loki must NOT use a named volume for the secret-adjacent store"


def test_loki_retention_is_enabled_and_bounded():
    """loki-config.yml turns the compactor retention ON and ties retention_period to ${LOKI_RETENTION_PERIOD},
    which loki.yaml ALWAYS supplies (default 720h). Guards the silent-infinite-retention / unbounded-disk
    failure (retention off by default; an empty period = forever) the must-fix #10 exists to prevent."""
    cfg = _load("docker/loki/loki-config.yml")
    assert cfg["compactor"]["retention_enabled"] is True, "compactor retention must be enabled (off by default)"
    period = cfg["limits_config"]["retention_period"]
    assert "LOKI_RETENTION_PERIOD" in str(period), "retention_period must come from the (always-set) env var"
    # the compose fragment must default the env so the expansion is never empty.
    env = _load("docker/services/loki.yaml")["services"]["loki"]["environment"]
    assert "720h" in str(env["LOKI_RETENTION_PERIOD"]), "loki.yaml must default LOKI_RETENTION_PERIOD"


def test_loki_and_vector_config_bind_sources_exist():
    """The loki/vector CONFIG bind-mount sources resolve (relative to docker/services/) to files/dirs that
    actually exist on disk. Guards the `../../` vs `../` path bug: a non-existent bind source is silently
    created by Docker as an empty DIRECTORY at runtime (Loki: 'is a directory'; Vector: 'config not found') —
    `docker compose config` validates structure but NOT that the source exists, so only a live run reveals it.
    Caught on the dogfood; this is its CI guard."""
    svc_dir = os.path.join(_ROOT, "docker", "services")
    for frag, svc in (("loki.yaml", "loki"), ("vector.yaml", "vector")):
        vols = _load(os.path.join("docker", "services", frag))["services"][svc]["volumes"]
        # the config mount is the one whose container target is /etc/<svc>... and whose source is relative (../)
        cfg = [v for v in vols if v.startswith("../")]
        assert cfg, "%s must mount its config via a ../ relative path" % frag
        for v in cfg:
            src = v.split(":")[0]
            resolved = os.path.normpath(os.path.join(svc_dir, src))
            assert os.path.exists(resolved), "%s bind source %r resolves to %r which does NOT exist" % (
                frag, src, resolved)


def test_vector_internal_source_transform_ids_disjoint_and_sink_resolves():
    """In the config.d/ implicit-namespacing tree, the source and transform component ids (= filename stems)
    are DISJOINT, and every loki-sink input names an existing transform. Guards two live-caught Vector errors:
    a source + transform sharing a name collide ('More than one component with name X'), and a transform
    consuming its own name is a cyclic dependency — and a sink consuming raw sources instead of shaped
    transforms. `docker compose config` can't see these (they're Vector-internal); this is the CI guard."""
    cfgd = os.path.join(_ROOT, "docker", "vector", "config.d")
    src_ids = {f[:-5] for f in os.listdir(os.path.join(cfgd, "sources")) if f.endswith(".yaml")}
    xfm_ids = {f[:-5] for f in os.listdir(os.path.join(cfgd, "transforms")) if f.endswith(".yaml")}
    assert not (src_ids & xfm_ids), "source/transform component-id collision: %s" % (src_ids & xfm_ids)
    sink = _load("docker/vector/config.d/sinks/loki.yaml")
    for inp in sink["inputs"]:
        assert inp in xfm_ids, "loki sink input %r is not a transform component (it must consume shaped events)" % inp


def _vrl_outside_strings(vrl):
    """A VRL program's lines that begin outside every block, bracket and literal (stripped); its code with literals
    and `#` comments blanked; and its text with only the comments taken out. VRL ignores indentation, so this counts
    depth: `{`, `(` and `[` open, their closers close, and nothing inside a literal counts: a `"…"` string, a raw
    string `s'…'`, a regex `r'…'` or a timestamp `t'…'`, each with backslash escapes (an escaped quote ends none)."""
    top, code, text, depth, quote, line, line_depth, i = [], [], [], 0, None, "", 0, 0
    while i < len(vrl):
        c = vrl[i]
        if quote:
            if c == "\\":
                line += vrl[i:i + 2]
                code.append("  ")
                text.append(vrl[i:i + 2])
                i += 2
                continue
            if c == quote:
                quote = None
            code.append("\n" if c == "\n" else " ")
        elif c == "#":
            end = vrl.find("\n", i)
            i = len(vrl) if end < 0 else end
            continue
        else:
            if c in "\"'":
                quote = c
            elif c in "{([":
                depth += 1
            elif c in "})]":
                depth -= 1
            code.append(c)
        text.append(c)
        if c == "\n":
            if line.strip() and line_depth == 0:
                top.append(line.strip())
            # a line that begins inside a multi-line literal is not top level, whatever it looks like
            line, line_depth = "", depth if quote is None else -1
        else:
            line += c
        i += 1
    if line.strip() and line_depth == 0:
        top.append(line.strip())
    return top, "".join(code), "".join(text)


# VRL that drops a field after it was set. Each shipped an event without it in Vector 0.56 (2026-10-21): an early
# `return`, `compact()` (which drops an empty `run_id`), `remove()`, `del(.)`, an assignment to the whole event
# (`. = …`, in a block too), and a fallible call with `!` (on its error the remap ships the event as it came in:
# `drop_on_error` is off). No shipped shape uses one, so the check refuses them outright rather than follow them.
_DROPS_FIELDS = re.compile(r"\breturn\b|\bcompact\(|\bremove!?\(|\w!\(|\bdel\(\s*\.\s*[,)]|(?<!\w)\.\s*=(?!=)")


def _label_fields_left_unset(vrl, fields):
    """The fields of `fields` a transform can leave unset on some event. A field is set on every event by an
    assignment that begins outside every block (`.f = …`), or by a one-line `if !exists(.f) { .f = … }` there, with
    no `del(.f)` or `del(."f")` anywhere (a `del` inside a string counts too: a false alarm, the safe direction). A
    program that uses `return`, `compact()`, `remove()`, `del(.)` or a fallible `!` call, or assigns the whole
    event, leaves every field in doubt."""
    top, code, text = _vrl_outside_strings(vrl)
    if _DROPS_FIELDS.search(code):
        return list(fields)
    unset = []
    for f in fields:
        sets = re.compile(r"^\.%s\s*=(?!=)" % f)
        default = re.compile(r"^if\s+!exists\(\.%s\)\s*\{\s*\.%s\s*=" % (f, f))
        deleted = re.search(r'\bdel\(\s*\.(?:%s\b|"%s")' % (f, f), text)
        if deleted or not any(sets.match(line) or default.match(line) for line in top):
            unset.append(f)
    return unset


def test_every_input_of_the_loki_sink_sets_each_field_it_templates_as_a_label():
    """Every transform the internal Loki sink reads sets each field the sink templates as a label
    (`"{{ field }}"`) on EVERY event: an assignment outside every block, or an `if !exists(.field)` default, and
    never a `del(.field)`. Guards the warning flood measured on both boxes and reproduced on a scratch Vector 0.56 +
    Loki 3.4.2 (2026-10-20): a field absent from an event fails its label template, so Vector logs a WARN per event
    (rate-suppressed) and ships the event without that label — `run_id` on container/journald/file lines, `level`
    on every plain-text container line. `vector validate` compiles the VRL but cannot see a field that is only
    set inside an `if`; this is the CI guard. (An empty `run_id` is stored by Loki as no label — measured — so the
    default changes no stream and no query.)"""
    sink = _load("docker/vector/config.d/sinks/loki.yaml")
    fields = sorted({f for v in sink["labels"].values()
                     for f in re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", str(v))})
    assert {"host", "service", "level", "run_id"} <= set(fields), "the sink templates the canonical labels"
    for inp in sink["inputs"]:
        vrl = _load("docker/vector/config.d/transforms/%s.yaml" % inp)["source"]
        unset = _label_fields_left_unset(vrl, fields)
        assert not unset, (
            "%s can leave the sink's label field(s) %s unset: assign each outside every block or add "
            "`if !exists(.f) { .f = ... }`, and never `del(.f)` (a missing field fails the label template on every "
            "event)" % (inp, unset))


def test_the_label_check_reads_vrl_structure_not_indentation():
    """The label check reads VRL by its braces, brackets, strings and comments, not by indentation. Why: VRL ignores
    indentation, and the check first read any line in column 0 as "outside every block". A default whose body sat
    unindented inside a conditional (`if !exists(.run_id) && contains(…) {` / `.run_id = ""` / `}`) passed it and
    both `vector test` cases, while a one-word container line still shipped without `run_id`; a later `del(.level)`
    inside an `if` passed too (round-1 review, 2026-10-21). A brace inside a string or a comment, or a call that
    spans lines, must not move the count. A line's depth is the depth where it begins, not where it ends. A round-3
    reader and the main thread got three valid programs past the walk (each run in Vector 0.56): in one an escaped
    quote in a regex literal (`r'it\\'s'`) ended the literal early, and in two a line that began inside a multi-line
    string was read as top level. The `closed_on_the_same_line` and `hash_in_a_string` rows kill that reader's
    surviving mutants of the walk. A comparison (`==`) sets nothing, and a default must set the field it tests
    (round-4 review)."""
    fields = ["level", "run_id"]
    ok = '.level = "info"\nif !exists(.run_id) { .run_id = "" }\n'
    assert _label_fields_left_unset(ok, fields) == []
    unindented_if = '.level = "info"\nif !exists(.run_id) && contains(string(.message) ?? "", " ") {\n.run_id = ""\n}\n'
    assert _label_fields_left_unset(unindented_if, fields) == ["run_id"]
    later_del = ok + 'if .service == "semaphore" {\ndel(.level)\n}\n'
    assert _label_fields_left_unset(later_del, fields) == ["level"]
    braces_in_text = '.message = "{ ( [ \\" {"\n.level = "info"  # a comment with { in it\n.run_id = ""\n'
    assert _label_fields_left_unset(braces_in_text, fields) == []
    call_over_lines = ("lv = parse_regex(.message,\n  r'level=(?P<l>[a-z]+)'\n) ?? {}\n"
                       '.level = "info"\nif !exists(.run_id) { .run_id = "" }\n')
    assert _label_fields_left_unset(call_over_lines, fields) == []
    inside_a_call = 'x = merge(.,\n{\n.run_id = ""\n})\n.level = "info"\n'
    assert _label_fields_left_unset(inside_a_call, fields) == ["run_id"]
    closed_on_the_same_line = '.level = "info"\nif .x == "y" {\n.run_id = "" }\n'
    assert _label_fields_left_unset(closed_on_the_same_line, fields) == ["run_id"]
    hash_in_a_string = '.level = "info"\n.m = "a # b {"\nif .x == "z" {\n.run_id = "set"\n}\n'
    assert _label_fields_left_unset(hash_in_a_string, fields) == ["run_id"]
    hash_then_a_long_string = '.level = "info"\n.m = "a # b"\n.t = "x\n.run_id = y"\n'
    assert _label_fields_left_unset(hash_then_a_long_string, fields) == ["run_id"]
    escaped_quote = 'x = r\'it\\\'s\'\nif .x == "y" {\n.run_id = ""\n}\n.level = "info"\n'
    assert "r'it\\'s'" in escaped_quote and _label_fields_left_unset(escaped_quote, fields) == ["run_id"]
    inside_a_string = '.message = "a\n.run_id = b"\n.level = "info"\n'
    assert _label_fields_left_unset(inside_a_string, fields) == ["run_id"]
    a_comparison = '.level = "info"\n.run_id == "x"\n'
    assert _label_fields_left_unset(a_comparison, fields) == ["run_id"]
    a_default_for_another_field = '.level = "info"\nif !exists(.run_id) { .runid = "" }\n'
    assert _label_fields_left_unset(a_default_for_another_field, fields) == ["run_id"]


def test_the_label_check_refuses_vrl_that_drops_a_set_field():
    """The label check refuses a program that can drop a label field after setting it: a quoted `del(."level")`, an
    early `return`, `compact()`, `remove()`, `del(.)`, a fallible `!` call, or an assignment to the whole event, at
    the top or in a block. Why: each passed the walk while Vector 0.56 shipped the event without the field (the
    round-3 and round-4 reviews and the main thread, 2026-10-21), and the sink then fails that label's template on
    every such event. On a `!` call's error the remap ships the event as it came in, with none of the fields it set.
    No shipped shape uses one, so refusing them costs nothing; any of them inside a string is text, not code, and
    does not count."""
    fields = ["level", "run_id"]
    ok = '.level = "info"\nif !exists(.run_id) { .run_id = "" }\n'
    assert _label_fields_left_unset(ok + 'del(."level")\n', fields) == ["level"]
    assert _label_fields_left_unset('.level = "info"\nif .x == "y" { return . }\n.run_id = ""\n', fields) == fields
    assert _label_fields_left_unset(ok + ". = compact(.)\n", fields) == fields
    assert _label_fields_left_unset(ok + '. = remove!(., ["run_id"])\n', fields) == fields
    assert _label_fields_left_unset(ok + '. = {"message": .message}\n', fields) == fields
    assert _label_fields_left_unset(ok + 'if .x == "y" { . = {"message": .message} }\n', fields) == fields
    assert _label_fields_left_unset(ok + 'if .x == "y" {\n. = {"message": .message}\n}\n', fields) == fields
    assert _label_fields_left_unset(ok + 'x = del(.)\n', fields) == fields
    assert _label_fields_left_unset(ok + 'if .x == "y" { del(.) }\n', fields) == fields
    assert _label_fields_left_unset(ok + '.p = parse_json!(.message)\n', fields) == fields
    assert _label_fields_left_unset(ok + '.note = "return compact(.) remove(x) del(.) parse_json!(y) . = z"\n',
                                    fields) == []


def test_every_logging_token_env_is_passed_through_to_vector():
    """Every env var a LOGGING source interpolates (a `${ENV}` in a logging/<m>.yml `source.auth.value` — the
    credentialed-pull token) is passed through to the Vector container in docker/services/vector.yaml as a
    `${ENV:-}` mapping. The token SHAPE/VALUE is data-driven (deploy-stack composes it from the unified
    config/secret-env.manifest.generated.yml); this one-line passthrough per logging token carries NO secret and
    NO shape (compose `include` can't merge an addition into an existing service, so it stays hand-authored —
    verified present here). Guards a credentialed-pull source losing its token env (Vector would interpolate an
    empty token) and a token VALUE ever landing in a committed file (only `${ENV}`; C12). Telemetry exporter env
    (PVE_EXPORTER_*) is deliberately NOT here — those feed the exporter container, whose fragment gen-exporters
    generates in full; only Vector-interpolated logging tokens need this hand-authored passthrough."""
    env = _load("docker/services/vector.yaml")["services"]["vector"].get("environment") or {}
    ref = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
    needed = set()
    for path in glob.glob(os.path.join(_ROOT, "logging", "*.yml")):
        doc = _load(os.path.relpath(path, _ROOT)) or {}
        auth_value = str(((doc.get("source") or {}).get("auth") or {}).get("value") or "")
        needed |= set(ref.findall(auth_value))
    assert needed, "the fleet ships at least one credentialed logging source (proxmox_api) — sanity check"
    for te in sorted(needed):
        assert te in env, "logging token env %s must be passed through in vector.yaml" % te
        assert ("${%s" % te) in str(env[te]), "%s must be an ${ENV} passthrough, never a literal value" % te


def test_vector_runs_non_root():
    """Vector runs NON-ROOT (`user:` set, not 0/root) — the C12 promotion-blocker fix — reads journald via the
    discovered systemd-journal GID (`group_add: ${KONTROLL_JOURNAL_GID}`), and carries no-new-privileges. CRITICAL
    (MF-1): `group_add` must NOT contain `1001` — that gid is group-read on the API/GUI TLS PRIVATE keys
    (0640 1001:1001), so joining it would hand a non-root Vector RCE those keys, re-opening the blast radius this
    fix closes. Guards a regression to root AND the gid-1001 over-grant returning."""
    v = _load("docker/services/vector.yaml")["services"]["vector"]
    user = str(v.get("user") or "")
    assert user and not user.startswith("0") and user.lower() != "root", "Vector must run non-root (got %r)" % user
    ga = [str(g) for g in (v.get("group_add") or [])]
    assert any("KONTROLL_JOURNAL_GID" in g for g in ga), "journald read needs the discovered systemd-journal GID"
    assert not any(g.strip() == "1001" for g in ga), \
        "MF-1: must NOT group_add 1001 (it over-grants read of the API/GUI TLS private keys)"
    assert "no-new-privileges:true" in (v.get("security_opt") or []), "keep no-new-privileges (guards the non-root switch)"


def test_vector_uses_socket_proxy_not_raw_socket():
    """Vector does NOT mount the raw /var/run/docker.sock (root-equivalent), and its docker_logs source targets the
    docker-socket-proxy over the kontroll net. Guards re-introducing the root-equivalent socket that makes a Vector
    RCE = host root (the C12 promotion blocker)."""
    vol = _load("docker/services/vector.yaml")["services"]["vector"].get("volumes") or []
    assert not any("/var/run/docker.sock" in str(m) for m in vol), "the raw docker socket must NOT be mounted into Vector"
    src = _load("docker/vector/config.d/sources/internal_containers.yaml")
    assert src["docker_host"] == "http://docker-socket-proxy:2375", "docker_logs must target the proxy, not the raw socket"


def test_socket_proxy_is_read_only_filtered():
    """The docker-socket-proxy fragment sets POST=0 (the whole Docker API is GET/HEAD-only ⇒ cannot create/exec/kill
    a container) and publishes NO host port (kontroll-net only, C3). It alone holds the raw socket (:ro). Guards the
    proxy being opened to writes (which would restore the root-equivalent surface) or exposed off the internal net."""
    p = _load("docker/services/docker-socket-proxy.yaml")["services"]["docker-socket-proxy"]
    assert str(p["environment"]["POST"]) == "0", "proxy POST must be 0 (GET/HEAD-only, no container-create)"
    assert not p.get("ports"), "the proxy must publish NO host port (reachable only on the kontroll net)"
    assert any("/var/run/docker.sock" in str(m) for m in p["volumes"]), "the proxy holds the raw socket (:ro)"


def test_vector_data_dir_is_a_writable_bind_not_a_root_owned_named_volume():
    """Vector's data_dir (/var/lib/vector) is a BIND MOUNT under KONTROLL_STORAGE_ROOT (deploy-stack creates it
    10002-owned), NOT a named volume — a named volume initialises root-owned, so the non-root Vector cannot write
    its journald checkpoint/buffers and the container exits 'Permission denied' (caught on the GATE-A dogfood).
    Guards a regression back to the root-owned named volume."""
    vec = _load("docker/services/vector.yaml")
    data = [m for m in vec["services"]["vector"]["volumes"] if m.endswith(":/var/lib/vector")]
    assert data, "vector must mount a data dir at /var/lib/vector"
    assert all(m.startswith("${KONTROLL_STORAGE_ROOT") for m in data), \
        "the /var/lib/vector data dir must be a KONTROLL_STORAGE_ROOT bind (10002-owned), not a named volume: %r" % data
    assert "vector-data" not in (vec.get("volumes") or {}), "the root-owned vector-data named volume must be gone"


def test_vector_uid_distinct_from_loki():
    """Vector's uid differs from Loki's (10001) and from the semaphore/api/gui uid (1001). A shared uid would let a
    Vector RCE read the Loki store at rest (the C8 crown-jewel) or the 1001 secrets — a silent regression with no
    other guard (MF-2). Guards a future uid edit silently colliding."""
    vu = str(_load("docker/services/vector.yaml")["services"]["vector"]["user"]).split(":")[0]
    lu = str(_load("docker/services/loki.yaml")["services"]["loki"]["user"]).split(":")[0]
    assert vu != lu, "Vector uid (%s) must differ from Loki's (%s) — else Vector can read the Loki store" % (vu, lu)
    assert vu != "1001", "Vector uid must differ from the semaphore/api/gui uid 1001"


def test_loki_sink_labels_are_the_canonical_non_secret_set():
    """The Vector loki sink's `labels:` keys are a subset of the canonical non-secret set
    {source,host,service,level,run_id,device}. Guards a high-cardinality or secret-bearing label leaking into
    Loki's unencrypted index (C12 hard label rule) — the sink is the single label choke-point."""
    sink = _load("docker/vector/config.d/sinks/loki.yaml")
    assert sink["type"] == "loki"
    keys = set(sink["labels"].keys())
    assert keys <= _CANONICAL_LABELS, "non-canonical/secret-risk label key(s): %s" % (keys - _CANONICAL_LABELS)


def test_socket_proxy_lives_on_its_own_network_with_vector_as_sole_peer():
    """Even GET/HEAD-only, the proxy's `GET /containers/<id>/json` returns a container's Config.Env — every secret
    the stack passes by environment. So the proxy must NOT be on the shared `kontroll` network where every service
    could reach it (2026-10-08 review, finding 4): it joins ONLY `kontroll-socket`, Vector joins it as the single
    consumer, no other fragment does, compose.yaml declares it, and deploy-stack creates it `internal` (no egress).
    Guards a fragment 'tidying' the proxy back onto the shared network, or a new service joining the private one."""
    proxy = _load("docker/services/docker-socket-proxy.yaml")["services"]["docker-socket-proxy"]
    assert proxy.get("networks") == ["kontroll-socket"], "the proxy joins ONLY its private network: %r" % proxy.get("networks")
    vector = _load("docker/services/vector.yaml")["services"]["vector"]
    assert "kontroll-socket" in (vector.get("networks") or []), "Vector must join the proxy's network to reach it"
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    joiners = []
    for path in sorted(glob.glob(os.path.join(root, "docker", "services", "*.yaml"))):
        for name, svc in (_load(os.path.relpath(path, root)).get("services") or {}).items():
            if "kontroll-socket" in (svc.get("networks") or []):
                joiners.append(name)
    assert sorted(joiners) == ["docker-socket-proxy", "vector"], "only the proxy and Vector may join kontroll-socket: %r" % joiners
    compose = _load("docker/compose.yaml")
    assert (compose.get("networks") or {}).get("kontroll-socket", {}).get("external") is True

    def _walk(tasks):
        for t in tasks or []:
            if isinstance(t, dict):
                yield t
                for k in ("block", "rescue", "always"):
                    yield from _walk(t.get(k))
    created = {}
    for play in _load("ansible/playbooks/deploy-stack.yml"):
        for section in ("pre_tasks", "tasks", "post_tasks"):
            for t in _walk(play.get(section)):
                net = t.get("community.docker.docker_network")
                if isinstance(net, dict):
                    created[net.get("name")] = net
    assert created.get("kontroll-socket", {}).get("internal") is True, \
        "deploy-stack must create kontroll-socket as an internal network: %r" % created


def _vector_health_command():
    test = _load("docker/services/vector.yaml")["services"]["vector"]["healthcheck"]["test"]
    return test


class _Health(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):  # noqa: N802 — the stdlib's name
        body = b'{"ok":true}' if self.status == 200 else b'{"ok":false}'
        self.send_response(self.status if self.path == "/health" else 404)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _probe(command, port):
    return subprocess.run(["bash", "-c", command.replace("127.0.0.1/8686", "127.0.0.1/%d" % port)],
                          capture_output=True, timeout=20).returncode


def test_vector_health_check_runs_a_program_its_image_ships():
    """Vector's compose health check is run by bash (exec form), never by wget or curl, and it asks Vector's own
    loopback API (127.0.0.1:8686, the address Vector's own config gives its API) for /health; and the image it
    runs in is a Debian tag.

    Why: the Vector image is Debian with neither wget nor curl. The check was `wget -qO- …/health`, which failed
    with `wget: not found` on every run, so every box showed Vector `unhealthy` from its first deploy on —
    found on the floating box's deploy, 2026-10-20 — and an unhealthy state that is always there hides the day
    Vector really stops. bash ships in the stock tag and in kontroll/vector (built FROM it, in lockstep); an
    alpine or distroless tag has no bash and would bring the always-`unhealthy` state back, as would an API moved
    off the address the check asks."""
    test = _vector_health_command()
    assert test[:3] == ["CMD", "bash", "-c"], \
        "the check must be run by bash, which the Debian image ships (got %r)" % test
    command = test[3]
    assert not re.search(r"\b(wget|curl)\b", command), "the Vector image ships neither wget nor curl"
    assert "/dev/tcp/127.0.0.1/8686" in command and "GET /health " in command, \
        "the check asks Vector's loopback API (8686, never published) for /health"
    image = re.search(r":-([^}]+)}", _load("docker/services/vector.yaml")["services"]["vector"]["image"]).group(1)
    assert image.endswith("-debian"), "the health check runs bash; a non-Debian tag (%s) ships none" % image
    assert _load("docker/vector/vector.yaml")["api"]["address"] == "127.0.0.1:8686", \
        "the health check asks 127.0.0.1:8686 — move both together"


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None, reason="runs the check with bash's /dev/tcp")
def test_vector_health_check_passes_on_200_and_fails_otherwise():
    """The health check's own command, run with bash against a local server, exits 0 when /health answers 200 and
    non-zero when it answers 503 or nothing listens.

    Why: the row above pins the program; this one proves the command works — a check that can never pass (the old
    `wget` in an image without it) or never fail (a probe that ignores the status) both leave the container's
    health state meaning nothing."""
    command = _vector_health_command()[3]
    for status, want_ok in ((200, True), (503, False)):
        handler = type("H%d" % status, (_Health,), {"status": status})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            rc = _probe(command, server.server_address[1])
        finally:
            server.shutdown()
            server.server_close()
        assert (rc == 0) is want_ok, "status %d: the check exited %d" % (status, rc)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed = sock.getsockname()[1]
    assert _probe(command, closed) != 0, "nothing listening must fail the check"
