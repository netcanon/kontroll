"""The socket proxy's start script, which edits the C12 filter's own HAProxy template before the image renders it.

WHY (the failures these guard). The script makes two edits:

1. **It limits container reads to the list, an inspect and the logs, and fails closed.** The stock CONTAINERS grant
   passes every GET/HEAD under `/containers`. Through the stock 0.3.0 proxy on a real dockerd (2026-10-21):
   - `/containers/<id>/archive` returned a file from the container (200), `%2F`-encoded too;
   - `/export`, `/stats`, `/top` and `/changes` answered 200;
   - a websocket `GET /containers/<id>/attach/ws?stdin=1` was upgraded (101): a write to the stdin of any container
     started with stdin open.

   Vector's docker_logs needs none of those. One deny line ahead of every allow line now passes only `json`,
   `<id>/json` and `<id>/logs` under `/containers/`; every other read got 403. If the line cannot go in, or the copy
   the script wrote is anything but the template plus that line, the proxy does not start.
2. **It routes the log streams to the no-timeout backend, and fails open.** The template holds every request to
   `timeout server 10m` except `/events`, which it routes to a `docker-events` backend with `timeout server 0`.
   Vector follows each container's `/containers/<id>/logs` for as long as the container runs, so the proxy cut
   every stream idle for ten minutes (`sD--`) and Vector logged an ERROR and re-attached (scratch box,
   2026-10-20). The script adds a `/logs` route to that backend. If it cannot, or its copy is anything but the
   limited template plus that line, the proxy starts without the route and says so.

Each edit is written beside the template and checked before it is moved over it, because busybox awk exits 0 on a
write it could not make: on a full disk the route step once moved an empty copy over the template, and HAProxy came
up with no listener (round-2 review, in the 0.3.0 image, 2026-10-21).

These tests run the script itself, from the compose file, against the pinned image's whole template, under each POSIX
shell found (busybox `sh` with busybox's own tools, where installed, is what the container runs). They then evaluate the
resulting filter, rule by rule, for the requests that matter. The exec line and the template are tied to the pinned
image, because overriding the entrypoint drops the image's CMD.
"""
import os
import re
import shutil
import subprocess
import urllib.parse

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVICE = os.path.join(ROOT, "docker", "services", "docker-socket-proxy.yaml")
TEMPLATE = "/usr/local/etc/haproxy/haproxy.cfg.template"
# What each pinned image runs as its ENTRYPOINT + CMD (`docker image inspect`). A new tag goes in only after
# inspecting it: 0.4.x renders /tmp/haproxy.cfg, so 0.3.0's exec line crash-loops it.
IMAGE_RUNS = {
    "tecnativa/docker-socket-proxy:0.3.0": "docker-entrypoint.sh haproxy -f /usr/local/etc/haproxy/haproxy.cfg",
}
EXEC = "exec " + IMAGE_RUNS["tecnativa/docker-socket-proxy:0.3.0"]
# Each pinned image's whole ENV (`docker image inspect --format '{{json .Config.Env}}' <image>`): what HAProxy's
# `env()` sees for a variable the compose file leaves unset. In 0.3.0 EVENTS, PING and VERSION are on and the other
# grants off; HAPROXY_VERSION is set too, and `-m bool` would read its leading 2 as true.
IMAGE_ENV = {
    "tecnativa/docker-socket-proxy:0.3.0": dict(item.split("=", 1) for item in [
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HAPROXY_VERSION=2.2.33",
        "HAPROXY_URL=https://www.haproxy.org/download/2.2/src/haproxy-2.2.33.tar.gz",
        "HAPROXY_SHA256=24f9eec04ee8d9e3652370be3db9852dec8aa650b3c8eeae969300c86b6fda5b", "ALLOW_RESTARTS=0",
        "ALLOW_STOP=0", "ALLOW_START=0", "AUTH=0", "BUILD=0", "COMMIT=0", "CONFIGS=0", "CONTAINERS=0",
        "DISABLE_IPV6=0", "DISTRIBUTION=0", "EVENTS=1", "EXEC=0", "GRPC=0", "IMAGES=0", "INFO=0", "LOG_LEVEL=info",
        "NETWORKS=0", "NODES=0", "PING=1", "PLUGINS=0", "POST=0", "SECRETS=0", "SERVICES=0", "SESSION=0",
        "SOCKET_PATH=/var/run/docker.sock", "SWARM=0", "SYSTEM=0", "TASKS=0", "VERSION=1", "VOLUMES=0"]),
}


# The tools the script runs (the rest are shell builtins), and `head` for a stand-in: the busybox leg's probe asks
# each what it is. Never a shell builtin (`printf`, `echo`, `test`): ash answers those itself, with no BusyBox banner.
BUSYBOX_TOOLS = ("awk", "chmod", "cmp", "grep", "head", "mv", "rm", "sed")


def _shells():
    """`sh` (or bash in POSIX mode), and busybox `sh` where installed: busybox's shell and tools, the host's build
    standing in for the image's. A host's busybox `sh` finds the host's tools on PATH (GNU sed and grep, mawk or
    gawk), unless it is built to prefer its applets, so `_env` gives that leg a PATH of busybox's applets alone."""
    found = []
    if shutil.which("sh"):
        found.append([shutil.which("sh")])
    elif shutil.which("bash"):
        found.append([shutil.which("bash"), "--posix"])
    if shutil.which("busybox"):
        found.append([shutil.which("busybox"), "sh"])
    return found


SHELLS = _shells()
each_shell = pytest.mark.parametrize(
    "shell", SHELLS, ids=[" ".join([os.path.basename(s[0]).split(".")[0]] + s[1:]) for s in SHELLS])

# Each pinned image's own template, whole and verbatim (`docker run --rm --entrypoint cat <image> <TEMPLATE>`;
# Apache-2.0, THIRD-PARTY-NOTICES.md). The tests run the script on all of it, so an edit keyed on any line of the
# filter shows up in the exact diff. A new tag goes in only with its template.
TEMPLATES = {
    "tecnativa/docker-socket-proxy:0.3.0": os.path.join(ROOT, "tests", "fixtures", "docker-socket-proxy", "0.3.0",
                                                        "haproxy.cfg.template"),
}
with open(TEMPLATES["tecnativa/docker-socket-proxy:0.3.0"], encoding="utf-8") as _fh:
    STOCK = _fh.read()
STOCK_LINES = STOCK.splitlines()
EVENTS_ROUTE = r"    use_backend docker-events if { path,url_dec -m reg -i ^(/v[\d\.]+)?/events }"
METHOD_FILTER = "    http-request deny unless METH_GET || { env(POST) -m bool }"

# What docker_logs asks the proxy for, each answered 200 through the scripted proxy on a real dockerd (2026-10-21).
VECTOR_NEEDS = [
    ("GET", "/containers/json"), ("GET", "/v1.43/containers/json?all=1"), ("GET", "/v1.43/containers/3cc70970/json"),
    ("HEAD", "/containers/3cc70970/json"), ("GET", "/containers/3cc70970/logs?follow=1&stdout=1"),
    ("GET", "/v1.47/containers/a_b.c-d/logs"), ("GET", "/events"), ("GET", "/_ping"), ("GET", "/version"),
]
# Container reads past the logs. The stock filter passed each to a real dockerd, which answered 200, 101 (the
# websocket attach with stdin), 301 (a `..` path, a redirect) or 404 (the upper-case path, and an id holding an
# encoded `?`, which dockerd does not route). The scripted proxy answered 403 to each.
CONTAINER_READS_PAST_LOGS = [
    ("GET", "/containers/3cc70970/archive?path=/etc/hostname"), ("HEAD", "/containers/3cc70970/archive?path=/"),
    ("GET", "/v1.43/containers/3cc70970/export"), ("GET", "/containers/3cc70970/attach/ws?stdin=1&stdout=1"),
    ("GET", "/containers/3cc70970/stats?stream=0"), ("GET", "/containers/3cc70970/top"),
    ("GET", "/containers/3cc70970/changes"), ("GET", "/containers/3cc70970%2Farchive?path=/etc/hostname"),
    ("GET", "/v1.43/containers/3cc70970/json/../archive?path=/etc/hostname"), ("GET", "/CONTAINERS/3cc70970/ARCHIVE"),
    ("GET", "/containers/../json"), ("GET", "/containers/abc%3Fx/json"),
]
# Refused by the stock filter already (POST=0, the grants left off); they must stay refused.
REFUSED = [("POST", "/containers/create"), ("GET", "/images/json"), ("GET", "/info"), ("GET", "/exec/abc/json")]


def _proxy():
    with open(SERVICE, encoding="utf-8") as fh:
        return yaml.safe_load(fh)["services"]["docker-socket-proxy"]


def _script():
    """The start script as the container runs it (compose's `$$` is a literal `$`)."""
    p = _proxy()
    assert p["entrypoint"] == ["/bin/sh", "-c"], "the proxy's start script must run as `sh -c <command>`"
    script = p["command"][0].replace("$$", "$")
    assert TEMPLATE in script and EXEC in script, "the script must edit the shipped template, then exec the entrypoint"
    return script


def _env(tmp_path, shell):
    """The environment a shell runs the script in: for busybox, a PATH of links to each of its applets and nothing
    else, so a tool busybox lacks fails `not found`, as it would in the container."""
    if not os.path.basename(shell[0]).startswith("busybox"):
        return None
    applets = tmp_path / "applets"
    if not applets.exists():
        applets.mkdir()
        listed = subprocess.run([shell[0], "--list"], capture_output=True, text=True, timeout=60).stdout.split()
        assert set(BUSYBOX_TOOLS) <= set(listed), listed
        for tool in listed:
            os.symlink(shell[0], applets / tool)
    return dict(os.environ, PATH=str(applets))


def _run(tmp_path, template_text, shell, prefix=""):
    tpl = tmp_path / "haproxy.cfg.template"
    tpl.write_text(template_text, encoding="utf-8", newline="\n")
    script = prefix + _script().replace(TEMPLATE, tpl.as_posix()).replace(EXEC, "echo reached-exec")
    r = subprocess.run(shell + ["-c", script], capture_output=True, text=True, timeout=60, cwd=str(tmp_path),
                       env=_env(tmp_path, shell))
    return r, tpl.read_text(encoding="utf-8")


def _routes(text):
    return [line for line in text.splitlines()
            if line.startswith("    use_backend docker-events if ") and "/logs }" in line]


def _added(stock, out):
    """The lines `out` has beyond `stock`, asserting `stock` survives in it whole and in order."""
    out_lines = out.splitlines()
    added = [i for i, line in enumerate(out_lines) if line not in stock.splitlines()]
    kept = [line for i, line in enumerate(out_lines) if i not in added]
    assert kept == stock.splitlines(), "the script changed or dropped a line of the template"
    return [out_lines[i] for i in added], out_lines


def _limited(tmp_path, shell):
    """The template with the limit in force and no route: where a restart's route step starts from (the script's
    own first start, its route line taken out)."""
    _, once = _run(tmp_path, STOCK, shell)
    (route,) = _routes(once)
    limited = once.replace(route + "\n", "")
    assert len(_added(STOCK, limited)[0]) == 1, "the first start added more than the limit and the route"
    return limited


_TERM = re.compile(r"\|\||!?\{ [^{}]* \}|!?METH_GET")


def _term(token, env, method, path):
    # The path patterns are HAProxy's (PCRE); Python's re reads them the same way only while they keep to what the
    # two share (classes like `[\d\.]`, groups, `?`, `+`, `*`, `|`, `^`, `$`). A pattern using a PCRE-only or POSIX
    # class could be misjudged here: check it against the image's haproxy before trusting this evaluator with it.
    negate = token.startswith("!")
    token = token.lstrip("!")
    by_env = re.fullmatch(r"\{ env\((\w+)\) -m bool \}", token)
    by_path = re.fullmatch(r"\{ path,url_dec -m reg -i (\S+) \}", token)
    if token == "METH_GET":
        hit = method in ("GET", "HEAD")  # HAProxy's METH_GET matches HEAD too
    elif by_env:
        name = by_env.group(1)
        assert name in env, "the filter reads %s, which neither the image's ENV nor the compose file sets" % name
        # HAProxy's `-m bool` reads a value's leading integer, so `true` or `yes` is false: a grant set that way is
        # off, and a limit gated that way is never applied (both measured, 2026-10-21), while `2.2.33` is true.
        # Hold every variable the filter reads to 0 and 1.
        assert env[name] in ("0", "1"), "%s=%r: HAProxy's -m bool reads only a leading integer; use 0 or 1" % (
            name, env[name])
        hit = env[name] == "1"
    elif by_path:
        hit = bool(re.search(by_path.group(1), urllib.parse.unquote(path.split("?")[0]), re.I))
    else:
        raise AssertionError("a filter term this evaluator does not know: %r" % token)
    return hit != negate


def _filter(text, method, path):
    """What the frontend's `http-request` rules do with one request, taken in order as HAProxy takes them: the first
    rule whose condition holds decides it. Terms are ANDed and `||` ORs the groups, as in HAProxy. The environment is
    the pinned image's whole ENV with the compose file's over it. An `http-request` line of any other form fails:
    this evaluator would otherwise pass over a rule it cannot judge."""
    env = dict(IMAGE_ENV[_proxy()["image"]], **{k: str(v) for k, v in _proxy()["environment"].items()})
    for line in text.split("frontend dockerfrontend\n", 1)[1].splitlines():
        rule = re.fullmatch(r"    http-request (allow|deny)(?: (if|unless) (.*))?", line)
        if not rule:
            assert not re.match(r"\s*http-request\b", line), "an http-request line this evaluator cannot read: %r" % (
                line)
            continue
        action, keyword, cond = rule.groups()
        if keyword is None:
            return action
        assert _TERM.sub("", cond).strip() == "", "a condition this evaluator cannot read: %r" % cond
        groups, current = [], []
        for token in _TERM.findall(cond):
            if token == "||":
                groups.append(current)
                current = []
            else:
                current.append(token)
        groups.append(current)
        holds = any(all(_term(t, env, method, path) for t in group) for group in groups)
        if holds == (keyword == "if"):
            return action
    return "allow"


def test_the_stock_filter_passes_the_container_reads_the_limit_exists_for():
    """Run over the stock template, the evaluator passes every container read past the logs that the stock proxy
    passed on a real dockerd, and still refuses what that proxy refused. Why: it shows the evaluator models the
    filter it reads. Then the next test's 403s say something about the script, not about a too-strict evaluator."""
    for method, path in VECTOR_NEEDS + CONTAINER_READS_PAST_LOGS:
        assert _filter(STOCK, method, path) == "allow", (method, path)
    for method, path in REFUSED:
        assert _filter(STOCK, method, path) == "deny", (method, path)


@each_shell
def test_the_filter_passes_what_vector_needs_and_no_other_container_read(tmp_path, shell):
    """After the script, the filter passes the list, an inspect, the logs, the events stream, _ping and version, and
    denies every other container read: archive (GET and HEAD, `%2F`-encoded too), export, the websocket attach,
    stats, top, changes, a `..` path, an id holding an encoded `?`. What the stock filter refused stays refused. Why:
    a container read past the logs reaches any container's files or its stdin, which a Vector RCE must not get; a
    limit that denied a read Vector makes would blank every container log."""
    r, out = _run(tmp_path, STOCK, shell)
    assert r.returncode == 0 and "reached-exec" in r.stdout, r.stdout + r.stderr
    for method, path in VECTOR_NEEDS:
        assert _filter(out, method, path) == "allow", (method, path)
    for method, path in CONTAINER_READS_PAST_LOGS + REFUSED:
        assert _filter(out, method, path) == "deny", (method, path)


@each_shell
def test_the_script_adds_the_read_limit_and_the_route_and_nothing_else(tmp_path, shell):
    """On the pinned image's whole template the script adds exactly two lines and keeps every other line as it was:
    an `http-request deny` on the line before the first allow line, and a `/logs` route to the `docker-events`
    backend on the line after the events route; then it reaches the entrypoint. Why: the script rewrites the
    read-only Docker API filter's own config, so a line it slipped in anywhere else (an `http-request allow`, say)
    would widen the C12 filter. HAProxy takes the first `http-request` rule that matches, so a limit placed after
    the CONTAINERS allow would limit nothing; it takes the first matching `use_backend` too, so a route on another
    backend keeps the 10-minute cut."""
    r, out = _run(tmp_path, STOCK, shell)
    assert r.returncode == 0 and "reached-exec" in r.stdout, r.stdout + r.stderr
    added, lines = _added(STOCK, out)
    assert len(added) == 2 and len(lines) == len(STOCK_LINES) + 2, added
    limit, route = added
    assert limit.startswith("    http-request deny if ") and lines[lines.index(METHOD_FILTER) + 1] == limit
    assert next(line for line in lines if line.startswith("    http-request allow ")) == lines[lines.index(limit) + 1]
    assert _routes(out) == [route] and lines[lines.index(EVENTS_ROUTE) + 1] == route
    assert "container reads limited" in r.stdout and "routed to the no-timeout backend" in r.stdout, r.stdout


@each_shell
def test_the_route_matches_log_streams_and_nothing_else(tmp_path, shell):
    """The added route's path regex matches a container's logs endpoint (with or without an API version) and not
    the other container reads docker_logs makes (list, inspect) or the events stream. Why: anything it matches
    loses the 10-minute server timeout; an inspect or list should still be cut if Docker hangs."""
    _, out = _run(tmp_path, STOCK, shell)
    rx = re.search(r"-m reg -i (\S+) \}", _routes(out)[0]).group(1)
    for path in ["/containers/3cc70970/logs", "/v1.43/containers/quiet-rid/logs", "/v1.47/containers/a_b.c-d/logs"]:
        assert re.search(rx, path, re.I), path
    for path in ["/containers/json", "/v1.43/containers/3cc70970/json", "/events", "/v1.43/events"]:
        assert not re.search(rx, path, re.I), path


@each_shell
def test_a_restart_finds_both_edits_and_adds_nothing(tmp_path, shell):
    """Run on a template it has already edited (a container restart keeps its edited file), the script adds nothing
    and says both edits are there. Why: a second copy per restart would grow the config on every restart."""
    _, once = _run(tmp_path, STOCK, shell)
    r, twice = _run(tmp_path, once, shell)
    assert twice == once
    assert r.stdout.count("already present") == 2 and "reached-exec" in r.stdout, r.stdout


@each_shell
def test_a_limit_line_after_the_allow_lines_is_not_taken_for_the_limit(tmp_path, shell):
    """A template that holds the limit's line after the CONTAINERS allow line, where it limits nothing, still gets
    the limit on the line before the first allow line. Why: "already present" must mean "in force". A check for the
    line anywhere would skip the edit and report the limit present while every container read passed."""
    _, once = _run(tmp_path, STOCK, shell)
    limit = _added(STOCK, once)[0][0]
    late = STOCK.replace("    http-request deny\n", limit + "\n    http-request deny\n")
    assert _filter(late, "GET", "/containers/3cc70970/archive") == "allow"
    r, out = _run(tmp_path, late, shell)
    assert "container reads limited" in r.stdout and "reached-exec" in r.stdout, r.stdout
    assert _filter(out, "GET", "/containers/3cc70970/archive") == "deny"


@each_shell
def test_another_line_naming_logs_is_not_taken_for_the_route(tmp_path, shell):
    """A template whose filter already names the logs endpoint in an allow line still gets the route, once, after
    the events route. Why: a loose "already present" check (any line containing `/logs }`) skipped the edit and
    printed that the route was there, so the ten-minute cuts came back under a log line that said otherwise."""
    allow = (r"    http-request allow if { path,url_dec -m reg -i ^(/v[\d\.]+)?/containers/[a-zA-Z0-9_.-]+/logs }"
             r" { env(LOGS) -m bool }")
    other = STOCK.replace("    http-request deny\n", allow + "\n    http-request deny\n")
    r, out = _run(tmp_path, other, shell)
    added, lines = _added(other, out)
    assert len(added) == 2 and lines[lines.index(EVENTS_ROUTE) + 1] == added[1], added
    assert "already present" not in r.stdout, r.stdout


@each_shell
def test_a_failed_write_of_the_limit_does_not_start_the_proxy(tmp_path, shell):
    """If the limited template cannot be written (here its path is a directory), the script prints an ERROR, leaves
    the template whole and exits non-zero without starting the proxy. Why: the limit is a security control, so it
    fails closed. A proxy started on the stock filter would pass every container read while the deploy reported
    nothing wrong. A stopped proxy blanks the container logs, which someone will notice."""
    (tmp_path / "haproxy.cfg.template.kontroll").mkdir()
    r, out = _run(tmp_path, STOCK, shell)
    assert out == STOCK
    assert r.returncode != 0 and "ERROR" in r.stdout and "reached-exec" not in r.stdout, r.stdout


@each_shell
def test_a_template_without_an_allow_line_does_not_start(tmp_path, shell):
    """If an image's template has no `http-request allow` line to put the limit before, the script prints an ERROR
    and does not start the proxy. Why: the same fail-closed rule; a filter whose layout the script does not know is
    one it cannot vouch for."""
    other = "".join(line for line in STOCK.splitlines(keepends=True) if not line.startswith("    http-request allow "))
    r, _ = _run(tmp_path, other, shell)
    assert r.returncode != 0 and "ERROR" in r.stdout and "reached-exec" not in r.stdout, r.stdout


@each_shell
def test_a_failed_write_of_the_route_starts_with_the_limit_and_says_so(tmp_path, shell):
    """With the limit in force (a restart), if the routed template cannot be written the script prints a WARNING, not
    the success line, leaves the template whole and still starts the proxy. Why: the route only spares the streams a
    ten-minute cut, so it fails open; a success line over a failed write would hide the cuts."""
    limited = _limited(tmp_path, shell)
    (tmp_path / "haproxy.cfg.template.kontroll").mkdir()
    r, out = _run(tmp_path, limited, shell)
    assert out == limited
    assert "WARNING" in r.stdout and "routed" not in r.stdout and "reached-exec" in r.stdout, r.stdout


# Stand-ins for the container's awk: a shell function ahead of the script, which a shell runs before anything on
# PATH or a busybox applet. Each damages only the copies the script writes (the awk calls that print a line from the
# environment), writes which step's copy it damaged to `stub-ran` (R the limit's, L the route's), and runs the real
# awk (`command awk`) for every other call. `$new` is the line the damaged copy adds.
AWK_STUBS = {
    # busybox awk exits 0 on a write it could not make: on a full disk (in the 0.3.0 image) the copy came out empty.
    "writes nothing": ":",
    # ... and on a disk that fills partway it stops short (4096 of about 4270 bytes, measured), still exiting 0
    "writes half": 'command awk "$@" | head -c 2048',
    # an edit gone wrong: the new line, and one allow line widened (`/images` passes under CONTAINERS=1 with IMAGES=0)
    "widens an allow line": 'command awk "$@" | sed "s/env(IMAGES)/env(CONTAINERS)/"',
    # ... or the new line written wrong (its last character) and every other line right
    "garbles the new line": 'R="${R%?}x" L="${L%?}x" command awk "$@"',
    # ... or every line right and the new one left out, or put on line 1 (a route there: "out of section" in HAProxy)
    "drops the new line": 'command awk "$@" | grep -vxF "$new"',
    "moves the new line to the top": 'printf "%s\\n" "$new"; command awk "$@" | grep -vxF "$new"',
}


def _stub_awk(stub):
    return ('awk() {\n  case "$1" in *"print ENVIRON"*) ;; *) command awk "$@"; return;; esac\n'
            '  case "$1" in *\'ENVIRON["L"]\'*) step=L new=$L;; *) step=R new=$R;; esac\n'
            '  echo "$step" >> stub-ran\n  %s\n  return 0\n}\n' % AWK_STUBS[stub])


def _damaged(tmp_path):
    """Which steps' copies a stand-in damaged, in order."""
    marks = tmp_path / "stub-ran"
    return marks.read_text(encoding="utf-8").split() if marks.exists() else []


@each_shell
@pytest.mark.parametrize("stub", sorted(AWK_STUBS))
def test_a_bad_copy_of_the_limit_does_not_start_the_proxy(tmp_path, shell, stub):
    """If the copy awk wrote for the limit is anything but the template plus the limit line, on the line before the
    first allow line, the script does not move it over the template: it removes it, prints an ERROR and exits
    non-zero without starting the proxy. Why: busybox awk exits 0 on a write it could not make, so without the check
    a full disk moved an empty copy in and HAProxy came up with no listener, and a short copy is the same. A copy
    that widened an allow line would pass every exact-diff test whose template lacked that line: the check holds
    the script to its one line, in its place, at run time as well. The stand-in must have damaged the limit's copy:
    with the awk call respelled, a row that passed without it would leave the check untested and the suite green
    (round-4 review, 2026-10-21)."""
    r, out = _run(tmp_path, STOCK, shell, prefix=_stub_awk(stub))
    assert _damaged(tmp_path) == ["R"], "the stand-in did not damage the limit's copy: the script's awk calls no " \
                                        "longer match it (%s)" % _damaged(tmp_path)
    assert out == STOCK
    assert r.returncode != 0 and "ERROR" in r.stdout and "reached-exec" not in r.stdout, r.stdout + r.stderr
    assert not (tmp_path / "haproxy.cfg.template.kontroll").exists(), "the rejected copy was left beside the template"


@each_shell
@pytest.mark.parametrize("stub", sorted(AWK_STUBS))
def test_a_bad_copy_of_the_route_starts_with_the_limit_and_says_so(tmp_path, shell, stub):
    """With the limit in force (a restart), if the copy awk wrote for the route is anything but the template plus the
    route line, on the line after the events route, the script removes it, prints a WARNING, not the success line,
    and starts the proxy on the limited template. Why: the same unreported write (round-2 review, 2026-10-21): an
    empty copy moved in left HAProxy with no listener, and a restart, finding no events route, kept it empty until a
    recreate. A route on line 1 passed a check that only counted it, and HAProxy then refused the config (round-4
    review). The stand-in must have damaged the route's copy, as above."""
    limited = _limited(tmp_path, shell)
    r, out = _run(tmp_path, limited, shell, prefix=_stub_awk(stub))
    assert _damaged(tmp_path) == ["L"], "the stand-in did not damage the route's copy: the script's awk calls no " \
                                        "longer match it (%s)" % _damaged(tmp_path)
    assert out == limited
    assert "WARNING" in r.stdout and "routed" not in r.stdout and "reached-exec" in r.stdout, r.stdout + r.stderr
    assert not (tmp_path / "haproxy.cfg.template.kontroll").exists(), "the rejected copy was left beside the template"


@each_shell
def test_the_busybox_leg_runs_busybox_tools(tmp_path, shell):
    """Under busybox `sh`, the PATH is busybox's applets alone, and each of BUSYBOX_TOOLS is a busybox applet: asked
    for `--help`, it answers as BusyBox. Why: a host's busybox `sh` finds the host's tools on PATH (GNU sed and grep,
    mawk or gawk), so the leg meant to run busybox's tools ran the shell alone; busybox awk's exit 0 on a failed
    write is what the copy checks exist for (round-4 review, 2026-10-21). With the applets alone on PATH, a tool the
    script gains runs busybox's copy or fails `not found` (a later review, 2026-10-21). The test asks each tool what it
    is, not where it was found: a busybox built to prefer its applets (Ubuntu's busybox-static, on GitHub's runner
    image) runs them whatever PATH says, and `command -v` names them bare. Another shell runs the host's tools, as
    it should."""
    r = subprocess.run(shell + ["-c", "for t in %s; do command -v $t; done" % " ".join(BUSYBOX_TOOLS)],
                       capture_output=True, text=True, timeout=60, env=_env(tmp_path, shell))
    found = r.stdout.split()
    assert len(found) == len(BUSYBOX_TOOLS), r.stdout + r.stderr
    if os.path.basename(shell[0]).startswith("busybox"):
        r = subprocess.run(shell + ["-c", 'echo "$PATH"'], capture_output=True, text=True, timeout=60,
                           env=_env(tmp_path, shell))
        assert r.stdout.strip() == str(tmp_path / "applets"), r.stdout + r.stderr
        ask = 'for t in %s; do printf "%%s " "$t"; $t --help 2>&1 | { read -r l; echo "$l"; }; done'
        r = subprocess.run(shell + ["-c", ask % " ".join(BUSYBOX_TOOLS)],
                           capture_output=True, text=True, timeout=60, env=_env(tmp_path, shell))
        answers = dict(line.split(" ", 1) for line in r.stdout.splitlines())
        assert sorted(answers) == sorted(BUSYBOX_TOOLS), r.stdout + r.stderr
        assert all(a.startswith("BusyBox v") for a in answers.values()), answers


@each_shell
def test_a_template_without_the_events_route_starts_with_the_limit_and_says_so(tmp_path, shell):
    """If an image's template no longer has the events route, the script still adds the limit, leaves the routing
    alone, prints a WARNING and starts the proxy. Why: the log streams then fall back to the 10-minute cut, which is
    noise; a proxy that failed to start over it would take every container log with it."""
    other = STOCK.replace(EVENTS_ROUTE, "    use_backend something-else if { path,url_dec -m reg -i ^/other }")
    r, out = _run(tmp_path, other, shell)
    added, _ = _added(other, out)
    assert len(added) == 1 and added[0].startswith("    http-request deny if "), added
    assert "WARNING" in r.stdout and "reached-exec" in r.stdout, r.stdout


def test_the_exec_line_is_the_pinned_images_cmd_and_its_template_and_env_are_recorded():
    """The script's last line execs exactly what the pinned image's ENTRYPOINT + CMD run, the template these tests
    edit is that image's own, and every variable that template reads has the image's ENV recorded. Why: overriding
    the entrypoint drops the image's CMD, so the script restates it; on a tag bump the restated path goes stale
    silently (0.4.x renders /tmp/haproxy.cfg, so 0.3.0's line makes HAProxy exit 1 and the proxy crash-loops, every
    container log blank). The exact-diff tests hold the script only to the template they run it on: a fixture that
    held part of the filter let an edit keyed on another line open every GET with every test green (round-2 review,
    2026-10-21). And the evaluator reads a variable the compose file leaves unset from the image's whole ENV,
    recorded per tag. A new tag fails here until someone inspects it and records what it runs, ships and sets."""
    p = _proxy()
    assert p["image"] in IMAGE_RUNS, (
        f"{p['image']}: re-inspect its ENTRYPOINT/CMD (`docker image inspect`) and record them in IMAGE_RUNS; "
        "0.4.x renders /tmp/haproxy.cfg")
    assert p["image"] in TEMPLATES, f"{p['image']}: vendor its {TEMPLATE} under tests/fixtures/docker-socket-proxy/"
    last = p["command"][0].rstrip().splitlines()[-1].strip()
    assert last == "exec " + IMAGE_RUNS[p["image"]], last
    assert STOCK.startswith("global\n") and "frontend dockerfrontend\n" in STOCK and EVENTS_ROUTE in STOCK_LINES
    assert METHOD_FILTER in STOCK_LINES
    assert p["image"] in IMAGE_ENV, "%s: record its whole Config.Env in IMAGE_ENV (`docker image inspect`)" % p["image"]
    unrecorded = sorted(set(re.findall(r"env\((\w+)\)", STOCK)) - set(IMAGE_ENV[p["image"]]))
    assert not unrecorded, "record the image's ENV for %s in IMAGE_ENV (`docker image inspect`)" % unrecorded
