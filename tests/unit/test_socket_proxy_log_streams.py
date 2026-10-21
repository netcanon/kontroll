"""The socket proxy routes Vector's log streams (`/containers/<id>/logs`) to its no-timeout backend, and only them.

WHY (the failure these guard): tecnativa/docker-socket-proxy's HAProxy template holds every request to `timeout server
10m` except `/events`, which it routes to a `docker-events` backend with `timeout server 0`. Vector's docker_logs
follows each container's `/containers/<id>/logs` for as long as the container runs, so the proxy cut every stream
idle for ten minutes (`sD--`) and Vector logged an ERROR and re-attached: once per quiet container every ten
minutes (scratch box, 2026-10-20). The proxy's start script now adds a `/logs` route to that backend before the
entrypoint renders the template. The script rewrites the C12 filter's own config, so these tests run that script
itself, from the compose file, against the pinned image's whole template, under each POSIX shell found (busybox
`sh`, where installed, is what the container runs): it adds that one line and nothing else, after the events
route, once; a restart leaves it alone; a failed write, a copy that is not the template plus that line in its place (an empty
one from a write awk did not report, or one with another line changed), or a template without the events route
starts the stock template, with a warning. The exec line is tied to the pinned image, because overriding the
entrypoint drops the image's CMD.
"""
import os
import re
import shutil
import subprocess

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


def _shells():
    """`sh` (or bash in POSIX mode), and busybox `sh` where installed: the container's shell and tools."""
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


def _run(tmp_path, template_text, shell, prefix=""):
    tpl = tmp_path / "haproxy.cfg.template"
    tpl.write_text(template_text, encoding="utf-8", newline="\n")
    script = prefix + _script().replace(TEMPLATE, tpl.as_posix()).replace(EXEC, "echo reached-exec")
    r = subprocess.run(shell + ["-c", script], capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
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


@each_shell
def test_the_script_adds_one_route_after_the_events_route_and_nothing_else(tmp_path, shell):
    """On the real template's lines the script adds exactly one line, a `/logs` route to the `docker-events` backend,
    on the line after the events route, keeps every other line as it was, then reaches the entrypoint. Why: the
    script rewrites the read-only Docker API filter's own config, so a line it slipped in anywhere else (an
    `http-request allow`, say) would widen the C12 filter with every other test still green; and HAProxy takes the
    first matching `use_backend`, so a route on any other backend keeps the 10-minute cut."""
    r, out = _run(tmp_path, STOCK, shell)
    assert r.returncode == 0 and "reached-exec" in r.stdout, r.stdout + r.stderr
    added, lines = _added(STOCK, out)
    assert added == _routes(out) and len(added) == 1, added
    assert len(lines) == len(STOCK_LINES) + 1
    assert lines[lines.index(EVENTS_ROUTE) + 1] == added[0]
    assert "routed to the no-timeout backend" in r.stdout, r.stdout


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
def test_a_restart_finds_the_route_and_adds_none(tmp_path, shell):
    """Run on a template that already has the route (a container restart keeps its edited file), the script adds
    nothing and says so. Why: a second copy per restart would grow the config on every restart."""
    _, once = _run(tmp_path, STOCK, shell)
    r, twice = _run(tmp_path, once, shell)
    assert twice == once
    assert "already present" in r.stdout and "reached-exec" in r.stdout, r.stdout


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
    assert len(added) == 1 and lines[lines.index(EVENTS_ROUTE) + 1] == added[0], added
    assert "already present" not in r.stdout, r.stdout


@each_shell
def test_a_failed_write_starts_the_stock_template_and_says_so(tmp_path, shell):
    """If the edited copy cannot be written (here its path is a directory), the template is left whole, the script
    prints a WARNING, not the success line, and still starts the proxy. Why: a success line over a failed write
    hides the ten-minute cuts, and a template truncated by a half-done write would survive restarts and crash-loop
    the proxy, blanking every container log."""
    (tmp_path / "haproxy.cfg.template.kontroll").mkdir()
    r, out = _run(tmp_path, STOCK, shell)
    assert out == STOCK
    assert "WARNING" in r.stdout and "routed" not in r.stdout and "reached-exec" in r.stdout, r.stdout


# Stand-ins for the container's awk: a shell function ahead of the script, which a shell runs before anything on
# PATH or a busybox applet. Each damages only the copy the script writes (the awk call that prints a line from the
# environment), leaves a mark, and runs the real awk (`command awk`) for every other call. `$new` is the line the
# copy adds.
AWK_STUBS = {
    # busybox awk exits 0 on a write it could not make: on a full disk (in the 0.3.0 image) the copy came out empty.
    "writes nothing": ":",
    # ... and on a disk that fills partway it stops short (4096 of about 4270 bytes, measured), still exiting 0
    "writes half": 'command awk "$@" | head -c 2048',
    # an edit gone wrong: the new line, and one allow line widened (`/images` passes under CONTAINERS=1 with IMAGES=0)
    "widens an allow line": 'command awk "$@" | sed "s/env(IMAGES)/env(CONTAINERS)/"',
    # ... or the new line written wrong (its last character) and every other line right
    "garbles the new line": 'R="${R%?}x" L="${L%?}x" command awk "$@"',
    # ... or every line right and the new one left out, or put on line 1, where HAProxy refuses it ("out of section")
    "drops the new line": 'command awk "$@" | grep -vxF "$new"',
    "moves the new line to the top": 'printf "%s\\n" "$new"; command awk "$@" | grep -vxF "$new"',
}


def _stub_awk(stub):
    return ('awk() {\n  case "$1" in *"print ENVIRON"*) ;; *) command awk "$@"; return;; esac\n  : > stub-ran\n'
            '  case "$1" in *\'ENVIRON["L"]\'*) new=$L;; *) new=$R;; esac\n  %s\n  return 0\n}\n' % AWK_STUBS[stub])


@each_shell
@pytest.mark.parametrize("stub", sorted(AWK_STUBS))
def test_a_copy_that_is_not_the_template_plus_the_route_is_not_moved_in(tmp_path, shell, stub):
    """If the copy awk wrote is anything but the template plus the one route line, on the line after the events
    route, the script does not move it over the template: it removes it, prints a WARNING, not the success line, and
    starts the proxy on the stock template. Why: busybox awk exits 0 on a write it could not make, so on a full disk
    the script moved an empty copy over the template, printed that the streams were routed, and HAProxy came up on
    an empty config with no listener. A restart found no events route and kept it empty, so every container log
    stayed blank until a recreate (round-2 review, in the 0.3.0 image, 2026-10-21); a disk that fills partway leaves
    a short copy the same way. An awk edit that also widened an allow line passed every exact-diff test whose
    template lacked that line, and a route on line 1 passed a check that only counted it (round-4 review): the check
    holds the script to its one line, in its place, at run time as well. The stand-in must have run: a row that
    passed without it (the script's awk call respelled) would leave the check untested with the suite green."""
    r, out = _run(tmp_path, STOCK, shell, prefix=_stub_awk(stub))
    assert (tmp_path / "stub-ran").exists(), "the stand-in never ran: the script's awk calls no longer match it"
    assert out == STOCK
    assert "WARNING" in r.stdout and "routed" not in r.stdout and "reached-exec" in r.stdout, r.stdout + r.stderr
    assert not (tmp_path / "haproxy.cfg.template.kontroll").exists(), "the rejected copy was left beside the template"


@each_shell
def test_a_template_without_the_events_route_starts_unchanged_and_says_so(tmp_path, shell):
    """If an image's template no longer has the events route, the script leaves the template alone, prints a
    WARNING and still starts the proxy. Why: the log streams then fall back to the 10-minute cut, which is noise; a
    proxy that failed to start would take every container log with it."""
    other = STOCK.replace(EVENTS_ROUTE, "    use_backend something-else if { path,url_dec -m reg -i ^/other }")
    r, out = _run(tmp_path, other, shell)
    assert out == other
    assert "WARNING the events route is gone" in r.stdout and "reached-exec" in r.stdout, r.stdout


def test_the_exec_line_is_the_pinned_images_cmd_and_its_template_is_the_fixture():
    """The script's last line execs exactly what the pinned image's ENTRYPOINT + CMD run, and the template these
    tests edit is that image's own. Why: overriding the entrypoint drops the image's CMD, so the script restates it;
    on a tag bump the restated path goes stale silently (0.4.x renders /tmp/haproxy.cfg, so 0.3.0's line makes
    HAProxy exit 1 and the proxy crash-loops, every container log blank). And the exact-diff tests hold the script
    only to the template they run it on: a fixture that held part of the filter let an edit keyed on another line
    open every GET with every test green (round-2 review, 2026-10-21). A new tag fails here until someone inspects
    it and records what it runs and ships."""
    p = _proxy()
    assert p["image"] in IMAGE_RUNS, (
        f"{p['image']}: re-inspect its ENTRYPOINT/CMD (`docker image inspect`) and record them in IMAGE_RUNS; "
        "0.4.x renders /tmp/haproxy.cfg")
    assert p["image"] in TEMPLATES, f"{p['image']}: vendor its {TEMPLATE} under tests/fixtures/docker-socket-proxy/"
    last = p["command"][0].rstrip().splitlines()[-1].strip()
    assert last == "exec " + IMAGE_RUNS[p["image"]], last
    assert STOCK.startswith("global\n") and "frontend dockerfrontend\n" in STOCK and EVENTS_ROUTE in STOCK_LINES
