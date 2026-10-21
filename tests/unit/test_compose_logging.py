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


def test_every_input_of_the_loki_sink_sets_each_field_it_templates_as_a_label():
    """Every transform the internal Loki sink reads sets each field the sink templates as a label
    (`"{{ field }}"`) on EVERY event: an unconditional top-level assignment, or an `if !exists(.field)` default.
    Guards the warning flood measured on both boxes and reproduced on a scratch Vector 0.56 + Loki 3.4.2
    (2026-10-20): a field absent from an event fails its label template, so Vector logs a WARN per event
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
        top = [line for line in vrl.splitlines() if line and not line[0].isspace()]   # outside every block
        for f in fields:
            sets = re.compile(r"^\.%s\s*=(?!=)" % f)
            default = re.compile(r"^if\s+!exists\(\.%s\)\s*\{\s*\.%s\s*=" % (f, f))
            assert any(sets.match(line) or default.match(line) for line in top), (
                "%s can leave the sink's label field %r unset: assign it at the top level or add "
                "`if !exists(.%s) { .%s = ... }` (a missing field fails the label template on every event)"
                % (inp, f, f, f))


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
