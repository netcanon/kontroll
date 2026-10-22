#!/usr/bin/env python3
"""scripts/stale-config.py — name the stack services whose read-only config changed since they last loaded it.

A service that reads a bound config only when it starts (Vector, Grafana's provisioning, Loki, Prometheus, the
exporters, Caddy) keeps the config it started with, and `docker compose up` recreates a container only when its
service definition changed. So a build that changed only Vector's transforms, or only Grafana's alert rules, left
both on the old ones until something restarted them (found on both boxes' redeploy, 2026-10-22). deploy-stack.yml
runs this after `up`, restarts the services it names, then runs it again with `--write` to record what they loaded.

A service's config is each read-only bind `docker compose config` resolves for it (every include, syntax and
interpolation, as compose itself reads them) whose source is either in the repo tree, file or directory, or a
FILE under the storage root (what the deploy puts there: the Caddyfile, Vector's key, CA bundle and known_hosts,
the GUI's age key). A directory under the storage root is a store (audit logs, TLS), and a source elsewhere is the
host's; neither is config. A source under both roots (one root inside the other) is the deeper root's. A bind
whose target the service names in its `kontroll.config.live` label (space-separated) is one it reloads on its own
(Grafana's dashboards, Loki's overrides, Prometheus's file_sd targets): a restart would only interrupt it. Its
digest covers every entry under those sources, the path and the content, never the mtime — the generators
rewrite their files on every run, so an mtime says nothing. A service named that the resolved stack does not hold
(one behind a compose profile, say) fails the run: its config could not be checked, and saying nothing would
read as "unchanged".

The stamp (`--stamp`) holds the digest each running service last loaded; it is root's and 0600, because a digest
of a file that holds a credential (the rendered snmp.yml) is a way to test guesses at it. For the same reason this
prints service names only, never a digest. `--write` merges: a service left out of a run keeps its record.
A service whose container `up` started in this run (`--started`, container names) loaded the config as it is: it
is recorded and never named.

Usage:  stale-config.py --stamp <file> --stack-dir <docker dir> --storage-root <dir> [--started c1,c2] <svc>...
        stale-config.py --stamp <file> --stack-dir <docker dir> --storage-root <dir> --write <svc>...
The first prints the stale services, one a line; the second prints `written` or `unchanged`. A service with no
config is never stale and is not recorded; one missing from the stamp (none yet, or added since) is stale.
"""
import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile

LIVE_LABEL = "kontroll.config.live"


def _under(path, root):
    path, root = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:                                     # another drive
        return False


def compose_config(stack_dir, env_file=None):
    """The stack as `docker compose config --format json` resolves it: the deploy's own project, file and env."""
    cmd = ["docker", "compose", "--project-directory", stack_dir, "--file", os.path.join(stack_dir, "compose.yaml"),
           "--env-file", env_file or os.path.join(stack_dir, ".env"), "config", "--format", "json"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:                                  # its message is not echoed: it may quote the .env
        sys.exit("stale-config: `docker compose config` failed (rc %d); run it by hand to see why" % r.returncode)
    return json.loads(r.stdout)


def _kind(source, roots):
    """`tree` or `store`, by the deepest root the source is under (a storage root inside the checkout holds stores,
    not tree config; a checkout under the storage root holds tree config), or None."""
    under = [k for k in ("tree", "store") if _under(source, roots[k])]
    return max(under, key=lambda k: len(os.path.abspath(roots[k])), default=None)


def services(config, repo_root, storage_root):
    """{service: [(kind, source), …]} for each service with config: kind `tree` or `store`."""
    roots = {"tree": repo_root, "store": storage_root}
    found = {}
    for name, svc in (config.get("services") or {}).items():
        labels = svc.get("labels") or {}
        live = str(labels.get(LIVE_LABEL, "")).split() if isinstance(labels, dict) else []
        live = [t.rstrip("/") or "/" for t in live]
        sources = []
        for v in svc.get("volumes") or []:
            if v.get("type") != "bind" or not v.get("read_only"):
                continue
            target, source = str(v.get("target", "")).rstrip("/") or "/", str(v.get("source", ""))
            if any(target == t or target.startswith(t + "/") for t in live):
                continue
            kind = _kind(source, roots)
            if kind == "tree" or (kind == "store" and os.path.isfile(source)):
                sources.append((kind, source))
        if sources:
            found[name] = sources
    return found


def _entry(h, path, rel):
    """One entry of a digest: its path and its type, and a regular file's content or a link's target. A link is
    not followed (the container resolves it in its own tree), and a FIFO or a socket is never opened."""
    h.update(b"F\0" + rel.replace(os.sep, "/").encode() + b"\0")
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        h.update(b"L\0" + os.readlink(path).encode() + b"\0")
    elif stat.S_ISREG(st.st_mode):
        with open(path, "rb") as fh:
            h.update(hashlib.sha256(fh.read()).digest())
    else:
        h.update(b"T%o\0" % stat.S_IFMT(st.st_mode))


def _digest(sources, roots):
    """One digest over every entry under the sources, in a fixed order. Each source is named by its kind and its
    path from that kind's root, so a checkout that moved holds the same config and a file moved between two
    sources is a change. The source itself is followed, as Docker follows it when it binds it."""
    h = hashlib.sha256()
    for kind, source in sorted(sources):
        rel = os.path.relpath(source, roots[kind]).replace(os.sep, "/")
        h.update(b"S\0" + kind.encode() + b":" + rel.encode() + b"\0")
        real = os.path.realpath(source)
        if os.path.isfile(real):
            _entry(h, real, "")
        elif os.path.isdir(real):
            for d, dirs, names in os.walk(real):
                dirs.sort()
                for n in sorted(dirs + names):
                    _entry(h, os.path.join(d, n), os.path.relpath(os.path.join(d, n), real))
        else:
            h.update(b"missing\0")
    return h.hexdigest()


def _read_stamp(path):
    try:
        with open(path, encoding="utf-8") as fh:
            stamp = json.load(fh)
    except (OSError, ValueError):
        return {}
    return stamp if isinstance(stamp, dict) else {}


def _write_stamp(path, stamp):
    """Write the stamp whole or not at all, 0600 from its first byte (mkstemp makes it so)."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".stale-config.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(stamp, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _started_services(config, containers):
    """The services whose container is among `containers` (the names `up` printed `Started` for)."""
    project = str(config.get("name", ""))
    out = set()
    for name, svc in (config.get("services") or {}).items():
        own = svc.get("container_name")
        default = re.compile(r"%s-%s-[0-9]+" % (re.escape(project), re.escape(name)))
        if any(c == own or (not own and default.fullmatch(c)) for c in containers):
            out.add(name)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--stamp", required=True, help="the file holding what each running service last loaded")
    ap.add_argument("--stack-dir", required=True, help="the docker/ dir the deploy runs compose from")
    ap.add_argument("--storage-root", required=True, help="the stack's storage root (KONTROLL_STORAGE_ROOT)")
    ap.add_argument("--started", default="", help="comma-separated containers `up` started in this run")
    ap.add_argument("--write", action="store_true", help="record the services' digests instead of comparing")
    ap.add_argument("--config-json", help=argparse.SUPPRESS)      # tests: a resolved config in place of compose
    ap.add_argument("--env-file", help=argparse.SUPPRESS)         # tests: the shipped .env.example
    ap.add_argument("service", nargs="*")
    args = ap.parse_args(argv)
    if args.config_json:
        with open(args.config_json, encoding="utf-8") as fh:
            config = json.load(fh)
    else:
        config = compose_config(args.stack_dir, args.env_file)
    absent = [s for s in args.service if s not in (config.get("services") or {})]
    if absent:                                             # names only: a service's name is not a secret
        sys.exit("stale-config: not in the stack as `docker compose config` resolves it (a profile?): %s"
                 % " ".join(absent))
    roots = {"tree": os.path.dirname(os.path.abspath(args.stack_dir)), "store": os.path.abspath(args.storage_root)}
    known = services(config, roots["tree"], roots["store"])
    now = {s: _digest(known[s], roots) for s in args.service if s in known}
    loaded = _read_stamp(args.stamp)
    if args.write:
        stamp = {s: d for s, d in loaded.items() if s in known}    # a service left out keeps its record
        stamp.update(now)
        if stamp == loaded:
            print("unchanged")
        else:
            _write_stamp(args.stamp, stamp)
            print("written")
        return 0
    started = _started_services(config, [c for c in args.started.split(",") if c])
    for s in args.service:
        if s in now and loaded.get(s) != now[s] and s not in started:
            print(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
