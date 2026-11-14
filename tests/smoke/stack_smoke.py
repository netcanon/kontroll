#!/usr/bin/env python3
"""The compose-up smoke, first version: install a throwaway stack on THIS host by the install guide's quickstart, as
written, and check that it came up.

.github/workflows/smoke.yml runs it on a fresh GitHub-hosted runner, nightly and by hand. A human can run the same
thing on a scratch VM nobody keeps, from a clean clone: `python3 tests/smoke/stack_smoke.py --throwaway`. It runs the
quickstart's own lines, so it installs Docker through sudo if Docker is missing, mints a control key and passwords
that die with the host, and brings the default stack up; at the end it takes the stack down with its volumes
(`--keep` leaves it up). It leaves the canonical, the instance overlay and the keys it minted. Never run it on a
control node you keep: it refuses without `--throwaway`, and refuses a tree that already has an `instance/`. Run it
as an account whose uid is not 1001 (the network service's; the install refuses such an operator, so the smoke refuses
first) and that is already in the docker group (else the quickstart's first line adds it, and the smoke must be run
again from a new login, as the guide says).

1. The quickstart, verbatim. The one fenced block in the blockquote under the "Compose-native quickstart" heading of
   docs/install-from-scratch.md: its command lines run in order from the repository root, with only the placeholders
   substituted (`<ip>` 127.0.0.1, `<domain>` example.test, `<age1…>` the public half of a break-glass key minted for
   the run, whose private half is deleted before anything runs) and the `$EDITOR` line dropped. A placeholder or a
   command the smoke does not know is refused before anything runs, so the guide and the smoke cannot drift apart
   silently: a guide edit that breaks the install turns the smoke red. `--print` prints the commands and runs nothing.
2. Containers. After a settle window, and once no healthcheck is still starting, every container of the stack's
   compose project (the deploy's own `--project-directory docker`) is running, has not restarted and is healthy where
   it has a healthcheck. A container whose restart policy is `no` and which exited 0 is a one-shot that finished, and
   passes. A project with no running container fails: a smoke is never green because nothing started. The containers
   are judged again after the port probes, so one that crashed while they ran is a fault too.
3. Ports. Every TCP port a running container publishes answers HTTP on it (on 127.0.0.1 for a port bound to every
   address) with a status from 200 to 399, or 401 (a login the smoke does not hold), within a bound.

Exit 0 when every check holds, 1 when a step or a check failed, 2 when the guide's block cannot be read or the run is
refused. On a failure it prints the project's containers, each one's log tail and the disk use, and last the
`SMOKE FAILED` line, with every value of the rendered docker/.env and any age secret key replaced by [redacted] in all
of it: the values are throwaway, but the log of a public workflow is public. The install's own output is printed as it
comes: it shows no minted value when its output is not a terminal, but compose names a docker/.env line it cannot
parse.
"""
import argparse
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GUIDE = os.path.join("docs", "install-from-scratch.md")
HEADING = "Compose-native quickstart"
MGMT_IP = "127.0.0.1"
DOMAIN = "example.test"
RECIPIENT = "<age1…>"
# The command lines the smoke runs: each must be one of these, or start with one and a space.
KNOWN_COMMANDS = ("sudo bash scripts/install-prereqs.sh", "scripts/kontroll-installer.sh")
# The one line it drops: the operator's edit of the overlay, which a fresh scaffold does not need.
DROPPED = "$EDITOR "
# The uid the network service runs as (SECURITY.md C10): the install refuses an operator who has it.
SERVICE_UID = 1001
# The deploy's own compose arguments (ansible/playbooks/deploy-stack.yml, "Bring up the selected stack service(s)"),
# relative to the repository root: the same project directory, so the same compose project.
STACK = ["docker", "compose", "--project-directory", "docker", "--file", os.path.join("docker", "compose.yaml"),
         "--env-file", os.path.join("docker", ".env")]
_PLACEHOLDER = re.compile(r"<[^<>\s]+>")
_AGE_SECRET = re.compile(r"AGE-SECRET-KEY-1[0-9A-Z]+")
# What would let one line run more than its one command, or more than its words: a separator, a pipe, a background or
# a subshell, an expansion, a redirection, a brace. Checked after the placeholders are replaced.
_SHELL_META = re.compile(r"[;&|`$<>(){}\n]")


class SmokeError(Exception):
    """A refusal or a failed step: its message says which, and main() exits with its code."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def quickstart_block(text):
    """The lines of the one fenced block in the blockquote whose heading names the quickstart, with the quote markers
    removed. Refused (exit 2): no such heading or more than one, or a blockquote that holds no fenced block or more
    than one."""
    lines = text.splitlines()
    heads = [i for i, line in enumerate(lines)
             if line.startswith(">") and line.lstrip("> ").startswith("#") and HEADING in line]
    if len(heads) != 1:
        raise SmokeError("%s: %d blockquote headings name %r; the smoke reads exactly one"
                         % (GUIDE, len(heads), HEADING), 2)
    quote = []
    for line in lines[heads[0]:]:
        if not line.startswith(">"):
            break
        quote.append(line[2:] if line.startswith("> ") else line[1:])
    fences = [i for i, line in enumerate(quote) if line.strip().startswith("```")]
    if len(fences) != 2:
        raise SmokeError("%s: the quickstart's blockquote holds %d fence lines; the smoke reads exactly one block"
                         % (GUIDE, len(fences)), 2)
    return quote[fences[0] + 1:fences[1]]


def _strip_comment(line):
    """`line` without a trailing shell comment: a `#` that starts a word outside quotes."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i].rstrip()
    return line.rstrip()


def commands(block, subs):
    """The command lines of the quickstart block, in order, each continuation joined and its placeholders replaced
    from `subs`; comment lines, blank lines and the `$EDITOR` line are skipped. Refused (exit 2): a placeholder not in
    `subs`, a command that is not one of KNOWN_COMMANDS, a line that holds a shell operator, an expansion or a
    redirection (so a known command cannot carry another), a comment or a blank line inside a continued command, a
    backslash the shell would not read as a continuation (a space, a tab or a comment after it, or one escaped), or a
    block that ends in a continuation or holds no command."""
    out, pending = [], ""
    for raw in block:
        line = raw.strip()
        if not line or line.startswith("#"):
            if pending:
                raise SmokeError("%s: a quickstart command is continued into a %s line"
                                 % (GUIDE, "blank" if not line else "comment"), 2)
            continue
        line = _strip_comment(line)
        if line.endswith("\\"):
            # The shell continues a line only at a backslash that is its last character and is not itself escaped
            # (an odd run of them); after a space, a tab or a comment, or as `\\`, the next line is a command.
            if not raw.endswith("\\") or (len(line) - len(line.rstrip("\\"))) % 2 == 0:
                raise SmokeError("%s: the quickstart line %r ends in a backslash the shell does not read as a "
                                 "continuation (a space, a tab or a comment after it, or a backslash escaped)"
                                 % (GUIDE, raw.strip()), 2)
            pending += line[:-1].rstrip() + " "
            continue
        line, pending = (pending + line).strip(), ""
        if line.startswith(DROPPED):
            continue
        for found in _PLACEHOLDER.findall(line):
            if found not in subs:
                raise SmokeError("%s: the quickstart line %r names the placeholder %s, which the smoke does not know"
                                 % (GUIDE, line, found), 2)
        for key, value in subs.items():
            line = line.replace(key, value)
        if not any(line == known or line.startswith(known + " ") for known in KNOWN_COMMANDS):
            raise SmokeError("%s: the quickstart line %r is not a command the smoke knows (%s)"
                             % (GUIDE, line, ", ".join(KNOWN_COMMANDS)), 2)
        meta = _SHELL_META.search(line)
        if meta:
            raise SmokeError("%s: the quickstart line %r holds the shell character %r; the smoke runs one plain "
                             "command per line, so a known command cannot carry another" % (GUIDE, line, meta.group()),
                             2)
        out.append(line)
    if pending:
        raise SmokeError("%s: the quickstart block ends inside a continued command" % GUIDE, 2)
    if not out:
        raise SmokeError("%s: the quickstart block holds no command" % GUIDE, 2)
    return out


def container_faults(records):
    """What is wrong with the project's containers, given `docker inspect` of each; [] when nothing is. An empty
    project, or one with no running container, is a fault: a smoke is never green because nothing started."""
    if not records:
        return ["the stack's compose project has no container: nothing started"]
    faults, running = [], 0
    for rec in records:
        name = (rec.get("Name") or "?").lstrip("/")
        state = rec.get("State") or {}
        restarts = rec.get("RestartCount") or 0
        policy = (((rec.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name")) or "no"
        health = (state.get("Health") or {}).get("Status")
        if state.get("Restarting") or state.get("Paused"):
            # Docker reports both as Running: a restart loop between two attempts, or a frozen process.
            faults.append("%s is %s (restarted %d time(s))" % (name, state.get("Status"), restarts))
        elif state.get("Running"):
            running += 1
            if restarts:
                faults.append("%s has restarted %d time(s): it crashed at least once" % (name, restarts))
            if health not in (None, "healthy"):
                faults.append("%s is running but its healthcheck says %s" % (name, health))
        elif policy == "no" and state.get("Status") == "exited" and state.get("ExitCode") == 0 and not restarts:
            continue
        else:
            faults.append("%s is %s (exit code %s, restart policy %s)"
                          % (name, state.get("Status"), state.get("ExitCode"), policy))
    if not running:
        faults.append("no container of the stack's compose project is running")
    return faults


def published_ports(records):
    """(container, host, port) for each TCP port a running container publishes, once per host and port; a port bound
    to every address (IPv4's or IPv6's) is reached on 127.0.0.1, and one bound only to another IPv6 address is left
    out (the stack's services bind every address or the management address, which the smoke sets to 127.0.0.1)."""
    seen, out = set(), []
    for rec in records:
        if not (rec.get("State") or {}).get("Running"):
            continue
        name = (rec.get("Name") or "?").lstrip("/")
        ports = (rec.get("NetworkSettings") or {}).get("Ports") or {}
        for key in sorted(ports):
            if not key.endswith("/tcp"):
                continue
            for bind in ports[key] or []:
                host_port = bind.get("HostPort")
                host = bind.get("HostIp") or "0.0.0.0"
                if not host_port:
                    continue
                host = MGMT_IP if host in ("0.0.0.0", "::") else host
                if ":" in host or (host, int(host_port)) in seen:
                    continue
                seen.add((host, int(host_port)))
                out.append((name, host, int(host_port)))
    return out


def status_ok(status):
    """Whether an HTTP status says the port is served: 200 to 399, or 401 (a login the smoke does not hold). None
    (no answer), a 5xx, and any other 4xx are not."""
    return status is not None and (200 <= status < 400 or status == 401)


def env_values(text):
    """The values of a rendered .env (KEY=VALUE lines, quotes removed) long enough to be a secret: eight characters
    or more. Shorter ones are left in the log; a value redacted that is no secret costs only readability."""
    values = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        value = line.split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if len(value) >= 8:
            values.add(value)
    return values


def redact(text, values):
    """`text` with each of `values`, longest first, and every age secret key replaced by [redacted]."""
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    return _AGE_SECRET.sub("[redacted]", text)


def _redact_env(text):
    """`text` redacted as the diagnostics are: every value of the rendered docker/.env, when there is one, and every
    age secret key."""
    try:
        with open(os.path.join(ROOT, "docker", ".env"), encoding="utf-8") as fh:
            values = env_values(fh.read())
    except OSError:
        values = set()
    return redact(text, values)


def _run(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kw)


def _mint_recipient():
    """The public half of an age key minted for this run; its private half is deleted before this returns."""
    if not shutil.which("age-keygen"):
        raise SmokeError("age-keygen is not installed: the smoke mints its throwaway break-glass recipient with it "
                         "(the workflow installs the age package first)", 2)
    tmp = tempfile.mkdtemp(prefix="smoke-bg-")
    try:
        key = os.path.join(tmp, "key")
        res = subprocess.run(["age-keygen", "-o", key], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res.returncode != 0:
            raise SmokeError("age-keygen failed (exit %d)" % res.returncode, 2)
        with open(key, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("# public key: age1"):
                    return line.split(":", 1)[1].strip()
        raise SmokeError("age-keygen wrote no public key line", 2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _inspect_project():
    ids = _run(STACK + ["ps", "--all", "--quiet"])
    if ids.returncode != 0:
        raise SmokeError("docker compose ps failed (exit %d):\n%s" % (ids.returncode, ids.stdout))
    names = ids.stdout.split()
    if not names:
        return []
    res = _run(["docker", "inspect"] + names)
    if res.returncode != 0:
        raise SmokeError("docker inspect failed (exit %d):\n%s" % (res.returncode, res.stdout))
    return json.loads(res.stdout)


def _http_status(host, port, timeout=10):
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/")
        return conn.getresponse().status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def _wait_settled(settle, bound):
    """`docker inspect` of the project once `settle` seconds have passed and no healthcheck is still starting, or once
    `bound` seconds have passed."""
    start = time.monotonic()
    while True:
        records = _inspect_project()
        elapsed = time.monotonic() - start
        starting = [r for r in records if ((r.get("State") or {}).get("Health") or {}).get("Status") == "starting"]
        if (elapsed >= settle and not starting) or elapsed >= bound:
            return records
        time.sleep(5)


def _port_faults(ports, bound=60):
    faults = []
    for name, host, port in ports:
        deadline, status = time.monotonic() + bound, None
        while True:
            status = _http_status(host, port)
            if status_ok(status) or time.monotonic() >= deadline:
                break
            time.sleep(3)
        print("    %s %s:%d -> %s" % (name, host, port, status if status is not None else "no answer"))
        if not status_ok(status):
            faults.append("%s publishes %s:%d, which answered %s" % (
                name, host, port, "nothing" if status is None else "HTTP %d" % status))
    return faults


def _diagnostics():
    """The disk use, then the project's containers and each one's log tail, redacted. With no rendered docker/.env
    there is no stack to show (and compose refuses the missing env file), so it says so instead; a `ps` that fails
    prints its error, never reads it as a list of container ids."""
    print("---- disk")
    print(_run(["df", "-h"]).stdout)
    print("---- containers")
    try:
        with open(os.path.join(ROOT, "docker", ".env"), encoding="utf-8") as fh:
            values = env_values(fh.read())
    except OSError:
        print("(no docker/.env: the stack was never rendered, so there is none to show)")
        return
    print(redact(_run(STACK + ["ps", "--all"]).stdout, values))
    ids = _run(STACK + ["ps", "--all", "--quiet"])
    if ids.returncode != 0:
        print(redact(ids.stdout, values))
        return
    for cid in ids.stdout.split():
        print("---- log of %s" % cid)
        print(redact(_run(["docker", "logs", "--tail", "80", cid]).stdout, values))


def _uid():
    return os.getuid() if hasattr(os, "getuid") else -1


def run(settle, keep):
    """The three steps; raises SmokeError on the first failed step or after the checks with their faults."""
    if os.path.exists(os.path.join(ROOT, "instance")):
        raise SmokeError("instance/ already exists: the smoke installs from a fresh clone only", 2)
    if _uid() == SERVICE_UID:
        raise SmokeError("this account has uid %d, the network service's, which the install refuses (local-canonical "
                         "cannot tell such an operator from the service): run the smoke as an account with another "
                         "uid" % SERVICE_UID, 2)
    with open(os.path.join(ROOT, GUIDE), encoding="utf-8") as fh:
        block = quickstart_block(fh.read())
    commands(block, {"<ip>": MGMT_IP, "<domain>": DOMAIN, RECIPIENT: "age1check"})   # refuse drift before acting
    lines = commands(block, {"<ip>": MGMT_IP, "<domain>": DOMAIN, RECIPIENT: _mint_recipient()})
    try:
        for n, line in enumerate(lines, 1):
            print("==> [%d/%d] %s" % (n, len(lines), line), flush=True)
            start = time.monotonic()
            res = subprocess.run(["bash", "-c", line], cwd=ROOT)
            print("    exit %d after %ds" % (res.returncode, time.monotonic() - start), flush=True)
            if res.returncode != 0:
                raise SmokeError("quickstart line %d failed (exit %d): %s" % (n, res.returncode, line))
        print("==> containers (settle %ds)" % settle, flush=True)
        records = _wait_settled(settle, settle + 240)
        for rec in records:
            state = rec.get("State") or {}
            print("    %s %s restarts=%s health=%s" % ((rec.get("Name") or "?").lstrip("/"), state.get("Status"),
                                                       rec.get("RestartCount"), (state.get("Health") or {}).get(
                                                           "Status", "-")))
        faults = container_faults(records)
        print("==> ports", flush=True)
        faults += _port_faults(published_ports(records))
        # Looked at once more after the probes, which can take a minute a port: a container that crashed meanwhile has
        # restarted, or is restarting, since the first look.
        faults += [f for f in container_faults(_inspect_project()) if f not in faults]
        if faults:
            raise SmokeError("the stack did not come up as it should:\n  - " + "\n  - ".join(faults))
        print("==> smoke green: %d container(s), %d port(s)" % (len(records), len(published_ports(records))))
    except SmokeError:
        _diagnostics()
        raise
    finally:
        if not keep and os.path.exists(os.path.join(ROOT, "docker", ".env")):
            _run(STACK + ["down", "--volumes", "--remove-orphans"])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--throwaway", action="store_true",
                    help="confirm this host is one nobody keeps (the smoke installs and removes a stack on it)")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="print the quickstart's commands as the smoke would run them, and run nothing")
    ap.add_argument("--keep", action="store_true", help="leave the stack up at the end")
    ap.add_argument("--settle", type=int, default=60, help="seconds to wait before judging the containers")
    args = ap.parse_args(argv)
    try:
        if args.print_only:
            with open(os.path.join(ROOT, GUIDE), encoding="utf-8") as fh:
                block = quickstart_block(fh.read())
            for line in commands(block, {"<ip>": MGMT_IP, "<domain>": DOMAIN, RECIPIENT: "age1-minted-at-run-time"}):
                print(line)
            return 0
        if not args.throwaway:
            raise SmokeError("refused: the smoke installs a stack on this host and removes it; pass --throwaway only "
                             "on a host nobody keeps (a CI runner, a scratch VM)", 2)
        run(args.settle, args.keep)
        return 0
    except SmokeError as exc:
        sys.stdout.flush()      # the diagnostics first, so the log's last line names the failure
        print("SMOKE FAILED: %s" % _redact_env(str(exc)), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
