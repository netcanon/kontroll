"""What the rows that run a network_cli play for real share: this machine's ports, where its Ansible finds the
network_cli connection, and whether it finds a plugin a row's play needs from a collection.

`serve_ports()` backs conftest.py's `ports` fixture: a port that takes no connection and one that takes each connection
and closes it at once (no SSH answers there), at a second loopback address where this machine has one. The rows that
run a play's port probe against them are tests/integration/test_backup_missed_runs.py (backup-configs.yml's probes,
I-65) and tests/integration/test_wire_logging_unreached_runs.py (the CLI backend's push, I-103).
"""
import functools
import json
import os
import socket
import subprocess
import threading


def _listener(address):
    """A socket bound to `address` at a free port, or None where this machine has no such address."""
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((address, 0))
    except OSError:
        server.close()
        return None
    return server


def serve_ports():
    """Yield the ports (`host`, `closed`, `plain`) while a thread accepts each connection to `plain` and closes it."""
    server = _listener("127.0.0.2") or _listener("127.0.0.1")
    host = server.getsockname()[0]
    probe = socket.socket()
    probe.bind((host, 0))
    closed = probe.getsockname()[1]
    probe.close()
    server.listen(16)
    server.settimeout(0.5)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield type("Ports", (), {"host": host, "closed": closed, "plain": server.getsockname()[1]})()
    finally:
        stop.set()
        thread.join(5)
        server.close()


@functools.lru_cache(maxsize=None)
def collections_path():
    """The collections path this machine's Ansible reads, as its own configuration sets it: a row's Ansible home is its
    own, which would hide the collections an image installs under the user's home."""
    p = subprocess.run(["ansible-config", "dump", "--format", "json"], capture_output=True, text=True, timeout=120)
    (row,) = [r for r in json.loads(p.stdout) if isinstance(r, dict) and r.get("name") == "COLLECTIONS_PATHS"]
    return os.pathsep.join(row["value"])


def has_plugins(env, *plugins):
    """Whether the Ansible of the environment `env` has each (`kind`, `name`) plugin, as `ansible-doc` finds it. A row
    whose play needs a collection's plugin skips, naming it, where the run's Ansible would not find it, rather than
    failing at the plugin's load: an image that installs its collections under root's home has none for another uid
    (I-76)."""
    for kind, name in plugins:
        p = subprocess.run(["ansible-doc", "-t", kind, "-j", name], env=env, capture_output=True, text=True,
                           timeout=120)
        if p.returncode != 0 or name not in p.stdout:
            return False
    return True


def has_network_cli(env, network_os):
    """Whether the Ansible of the environment `env` has the network_cli connection and `network_os`'s cliconf."""
    return has_plugins(env, ("connection", "ansible.netcommon.network_cli"), ("cliconf", network_os))
