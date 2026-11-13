"""The stand-in `ssh` that the rows which run a fleet play over ssh-family hosts put first on PATH, so a host's
connection is answered (or refused) on this machine whether or not it has an ssh client (the validate image has none).

For a host whose address is in $FAKESSH_UP (a comma list) it runs the remote command here, under /bin/sh, as an ssh
login would on the device; for any other it answers as a refused connection does (exit 255), so ansible-core counts
the host unreachable. A host in $FAKESSH_CALLS (`address=N`, comma separated) answers its first N connections and
refuses the rest, as one that drops mid-run does, each connection counted in a file under $FAKESSH_SEEN. The play's
ssh connection must pipeline (`[ssh_connection] pipelining = True`): the stand-in runs a command, and copies no file,
so a module comes on its stdin. The rows that use it are tests/integration/test_fleet_play_failure_cleans_up.py
(install-node-exporter.yml's fact gathering and re-check, wire-logging.yml's probe and re-check: I-89, I-92) and
tests/integration/test_wire_logging_allowlist_runtime.py (the wiring play's probe of its ssh hosts).
"""
import os
import sys

SCRIPT = """#!%s
import os, sys
argv, i, host, cmd = sys.argv[1:], 0, None, []
WITH_ARG = set("BbcDEeFIiJLlmOoPpQRSWw")
while i < len(argv):
    a = argv[i]
    if a.startswith("-") and len(a) > 1:
        i += 2 if a[1] in WITH_ARG and len(a) == 2 else 1
        continue
    host, cmd = a, argv[i + 1:]
    break
calls, over = dict(x.split("=") for x in os.environ.get("FAKESSH_CALLS", "").split(",") if x), False
if host in calls:
    seen = os.path.join(os.environ["FAKESSH_SEEN"], host)
    over = os.path.exists(seen) and os.path.getsize(seen) >= int(calls[host])
    with open(seen, "a") as fh:
        fh.write("x")
if over or host is None or host not in os.environ.get("FAKESSH_UP", "").split(","):
    sys.stderr.write("ssh: connect to host %%s port 22: Connection refused\\n" %% host)
    sys.exit(255)
os.execv("/bin/sh", ["/bin/sh", "-c", " ".join(cmd)])
""" % sys.executable


def install(bin_dir):
    """Write the stand-in into the existing directory `bin_dir` as an executable `ssh`, and return the directory
    (to put first on PATH)."""
    path = os.path.join(str(bin_dir), "ssh")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(SCRIPT)
    os.chmod(path, 0o755)
    return str(bin_dir)
