"""Every way a fleet playbook is run renders a key-auth host's SSH key from SOPS before its connections and removes it
after: a real run, a dry run (`--check`, `--check --diff`) and a tag-limited run (`--tags`) (I-87, I-93).

WHY. `_render-ssh-keys.yml` writes each key-auth host's private key, decrypted from its SOPS domain, to a 0600 file in
the run's key dir, which the host's `ansible_ssh_private_key_file` names, and `_cleanup-ssh-keys.yml` removes the dir
after the run's connections. In a dry run the dir and the copy were skipped, as a change is, so no key was written:
every key-auth host's connection named a key file that did not exist, could not log in, and counted as an unreachable
host, which the fleet plays ignore, so the `--check --diff` dry-run CLAUDE.md requires before a state-changing play
passed having reached no key-auth device (I-87; measured on ansible-core 2.20.10 against a key-only SSH server:
`Permission denied (publickey)`, rc 0). The cleanup's delete was skipped too, so a stale key an earlier run left
stayed. And neither play was tagged, so a `--tags` run skipped both (I-93): no key for a tagged device task (the
shipped playbooks have none: each reaches its class by an untagged include; an operator's playbook that imports the
preamble may), and the dir an earlier run left stayed. A `--skip-tags always` run still renders and removes none; its
row holds that, as the plays' headers say. A dry run's key dir is the real one with `.check` appended, so a dry run
neither removes the keys of a real run it overlaps nor logs in with them (the I-87 reads: a dry run's cleanup removed
a running job's keys, which then ended green with its key-auth host not run). And the key dir must hold nothing but
`*.key` files: the cleanup removes it whole, so a `KONTROLL_SSH_KEY_DIR` naming a dir that holds anything else (a home
dir, `~/.ssh`) lost all it held, in a real run and, after I-87's first commit, a dry one; such a path is refused by
the render and by the cleanup, and kept (I-95).

The rows run a probe playbook beside the shipped ones, in a copy of ansible/playbooks: the shipped preamble, a play over
the key-auth host whose one task (a `ping`, tagged `probe`) opens its connection, and the shipped cleanup. The
inventory's one host is key-auth (`kontroll_ssh_key_domain`) on the ssh connection, named with a hyphen, as the onboard
writes it: its key file and its SOPS field use the name with `_` (scripts/kontroll/service/onboard.py's `cred_key`).
Its key is a made-up text, not a key, encrypted with the installed `sops` to an age recipient made for the row from a
fresh X25519 key. `ssh` is a stand-in first on PATH that acts as a key-only server would: it logs in only when the
IdentityFile it is given exists, is readable by its owner alone and holds that key, and then runs the command on the
row's own machine (pipelined, so the module comes on its stdin); else it answers as a refused key does (`Permission
denied (publickey)`, exit 255). It records what it was given. The real run's key dir and the dry run's each hold a
stale key before each run but the first-run rows', which start with no key dir at all.

It needs ansible-playbook, sops, the community.sops collection and the `cryptography` package (ansible-core's own):
tests/validate.sh runs it as a step of its own that no skip passes (the validate job installs all four); elsewhere it
is skipped. Nothing of the developer's Ansible or SOPS reaches the play: its configuration, Ansible home, local and
remote temporary directories, ssh control-path directory, inventory, key dirs and age identity are the row's own.
"""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys

import pytest
import yaml

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not all(shutil.which(tool) for tool in ("ansible-playbook", "ansible-galaxy", "sops")),
                       reason="needs ansible-playbook, ansible-galaxy and sops on PATH — tests/validate.sh runs it "
                              "where they are"),
]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOST = "dev-1"
CRED = "dev_1"   # the onboard's cred_key: the host's name with `_` for `-`, in its key file's name and its SOPS field
# The stand-in key the SOPS domain holds: two lines of text, not a key (nothing here can log in anywhere).
KEY = "stand-in key text for the key render rows, not a key\nits second line\n"
PING = "Ping the key-auth host over its connection"
GUARD = "Refuse a key dir that holds anything but SSH keys (the cleanup removes it whole)"
PROBE = [
    {"name": "Fleet preamble", "ansible.builtin.import_playbook": "_fleet-preamble.yml"},
    {"name": "Reach the key-auth host", "hosts": "g", "gather_facts": False, "ignore_unreachable": True,
     "tasks": [{"name": PING, "ansible.builtin.ping": None, "tags": ["probe"]}]},
    {"name": "Clean up", "ansible.builtin.import_playbook": "_cleanup-ssh-keys.yml"},
]
CLEANUP_ONLY = [{"name": "Clean up", "ansible.builtin.import_playbook": "_cleanup-ssh-keys.yml"}]
FAKE_SSH = """#!%s
import hashlib, json, os, stat, sys
argv, i, host, cmd, identity = sys.argv[1:], 0, None, [], None
WITH_ARG = set("BbcDEeFIiJLlmOoPpQRSWw")
while i < len(argv):
    a = argv[i]
    if a == "-o" and i + 1 < len(argv) and argv[i + 1].startswith("IdentityFile="):
        identity = argv[i + 1].split("=", 1)[1].strip('"')
    if a.startswith("-") and len(a) > 1:
        i += 2 if a[1] in WITH_ARG and len(a) == 2 else 1
        continue
    host, cmd = a, argv[i + 1:]
    break
seen = {"host": host, "identity": identity, "exists": bool(identity) and os.path.isfile(identity)}
if seen["exists"]:
    seen["mode"] = stat.S_IMODE(os.stat(identity).st_mode)
    seen["dir_mode"] = stat.S_IMODE(os.stat(os.path.dirname(identity)).st_mode)
    with open(identity, "rb") as fh:
        seen["sha256"] = hashlib.sha256(fh.read()).hexdigest()
with open(os.environ["FAKESSH_LOG"], "a") as fh:
    fh.write(json.dumps(seen) + "\\n")
if not (seen["exists"] and seen["mode"] & 0o077 == 0 and seen["sha256"] == os.environ["FAKESSH_KEY_SHA256"]):
    sys.stderr.write("%%s: Permission denied (publickey).\\n" %% host)
    sys.exit(255)
os.execv("/bin/sh", ["/bin/sh", "-c", " ".join(cmd)])
""" % sys.executable


def _age_recipient_module():
    spec = importlib.util.spec_from_file_location("_age_recipient",
                                                  os.path.join(ROOT, "tests", "unit", "_age_recipient.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sops_collections():
    """The collections path that holds the installed community.sops (as the developer's Ansible finds it), which the
    row's own configuration names; skipped where there is none."""
    p = subprocess.run(["ansible-galaxy", "collection", "list", "community.sops", "--format", "json"],
                       capture_output=True, text=True, timeout=300)
    try:
        found = json.loads(p.stdout or "{}")
    except ValueError:
        found = {}
    paths = [os.path.dirname(path) for path, held in found.items() if "community.sops" in (held or {})]
    if not paths:
        pytest.skip("needs the community.sops collection — tests/validate.sh runs it where it is installed")
    return paths[0]


@pytest.fixture
def lab(tmp_path, sops_collections):
    """The tree copy with the probe and cleanup-only playbooks, the key-auth inventory, the SOPS domain holding KEY
    (encrypted to a recipient made here), the age identity (for sops alone), the real run's key dir and the dry run's
    (`.check`) each holding a stale key, the stand-in ssh and the row's Ansible configuration and environment."""
    x25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.x25519")
    serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    bech32 = _age_recipient_module().bech32
    private = x25519.X25519PrivateKey.generate()
    raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    identity = tmp_path / "age-identity.txt"
    identity.write_text(bech32("age-secret-key-", raw).upper() + "\n", encoding="utf-8")
    os.chmod(identity, 0o600)

    tree = tmp_path / "tree" / "ansible"
    shutil.copytree(os.path.join(ROOT, "ansible", "playbooks"), str(tree / "playbooks"))
    shutil.copytree(os.path.join(ROOT, "ansible", "filter_plugins"), str(tree / "filter_plugins"))
    (tree / "playbooks" / "zz-probe-keys.yml").write_text(yaml.safe_dump(PROBE, sort_keys=False), encoding="utf-8")
    (tree / "playbooks" / "zz-cleanup-only.yml").write_text(yaml.safe_dump(CLEANUP_ONLY, sort_keys=False),
                                                            encoding="utf-8")
    inventory = tmp_path / "inst" / "inventory"
    secrets = tmp_path / "inst" / "secrets"
    inventory.mkdir(parents=True)
    secrets.mkdir()
    (inventory / "hosts.yml").write_text(yaml.safe_dump({"all": {"children": {"g": {"hosts": {HOST: {
        "ansible_host": "192.0.2.40", "ansible_connection": "ssh", "ansible_user": "tester",
        "ansible_python_interpreter": "{{ ansible_playbook_python }}", "kontroll_ssh_key_domain": "lab",
        "ansible_ssh_private_key_file": "{{ kontroll_device_key_dir }}/%s.key" % CRED}}}}}}), encoding="utf-8")
    plain = tmp_path / "lab.plain.yml"
    plain.write_text(yaml.safe_dump({"%s_ssh_private_key" % CRED: KEY}), encoding="utf-8")
    enc = subprocess.run(["sops", "--encrypt", "--age", bech32("age", public), "--input-type", "yaml",
                          "--output-type", "yaml", str(plain)], cwd=str(tmp_path), capture_output=True, text=True,
                         timeout=120)
    plain.unlink()
    assert enc.returncode == 0, enc.stderr
    assert "stand-in key text" not in enc.stdout, enc.stdout
    (secrets / "lab.sops.yml").write_text(enc.stdout, encoding="utf-8")

    keys = tmp_path / "keys"
    check_keys = tmp_path / "keys.check"
    for held in (keys, check_keys):
        held.mkdir(mode=0o700)
        (held / "stale.key").write_text("not a key: a stale file an earlier run left\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text(FAKE_SSH, encoding="utf-8")
    os.chmod(bin_dir / "ssh", 0o755)
    home = tmp_path / "ansible-home"
    home.mkdir()
    cfg = home / "ansible.cfg"
    cfg.write_text("[defaults]\ncollections_path = %s\nfilter_plugins = %s\nretry_files_enabled = False\n"
                   "interpreter_python = auto_silent\nhost_key_checking = False\n[ssh_connection]\npipelining = True\n"
                   % (sops_collections, tree / "filter_plugins"), encoding="utf-8")
    log = tmp_path / "ssh-seen.jsonl"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "KONTROLL_", "SOPS_"))}
    env.update(ANSIBLE_CONFIG=str(cfg), ANSIBLE_HOME=str(home), ANSIBLE_LOCAL_TEMP=str(home / "tmp"),
               ANSIBLE_REMOTE_TEMP=str(home / "rtmp"), ANSIBLE_SSH_CONTROL_PATH_DIR=str(home / "cp"),
               ANSIBLE_NOCOLOR="1", ANSIBLE_DEPRECATION_WARNINGS="0", ANSIBLE_LOCALHOST_WARNING="0",
               KONTROLL_SSH_KEY_DIR=str(keys), SOPS_AGE_KEY_FILE=str(identity), FAKESSH_LOG=str(log),
               FAKESSH_KEY_SHA256=hashlib.sha256(KEY.encode()).hexdigest(), PYTHONDONTWRITEBYTECODE="1",
               PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    return type("Lab", (), {"tree": str(tree), "inventory": str(inventory / "hosts.yml"), "keys": str(keys),
                            "check_keys": str(check_keys), "base": str(tmp_path), "log": str(log), "env": env})()


def _run(lab, *args, playbook="zz-probe-keys.yml", key_dir=None, env_add=None):
    """Run the probe playbook (or `playbook`; with `args`, `key_dir` as KONTROLL_SSH_KEY_DIR when given and `env_add`
    over the row's environment); return its exit code, its output, the host's recap counts and what the stand-in ssh
    was given, one entry per call."""
    env = dict(lab.env, **(env_add or {}))
    if key_dir is not None:
        env["KONTROLL_SSH_KEY_DIR"] = key_dir
    p = subprocess.run(["ansible-playbook", "-i", lab.inventory, "playbooks/" + playbook, *args],
                       cwd=lab.tree, env=env, capture_output=True, text=True, timeout=600)
    out = p.stdout + p.stderr
    m = re.search(r"^%s\s*:\s*ok=\d+.*?\bunreachable=(\d+)\s+failed=(\d+)\b.*?\bignored=(\d+)" % re.escape(HOST),
                  p.stdout, re.M)
    assert m, out
    seen = []
    if os.path.exists(lab.log):
        with open(lab.log, encoding="utf-8") as fh:
            seen = [json.loads(line) for line in fh if line.strip()]
    counts = {"unreachable": int(m.group(1)), "failed": int(m.group(2)), "ignored": int(m.group(3))}
    return p.returncode, out, counts, seen


def _dirs(lab, args):
    """The key dir a run with `args` renders into, and the other mode's: a dry run's is the real one's with `.check`."""
    return (lab.check_keys, lab.keys) if "--check" in args else (lab.keys, lab.check_keys)


@pytest.mark.parametrize("args", [[], ["--check"], ["--check", "--diff"], ["--tags", "probe"],
                                  ["--check", "--diff", "--tags", "probe"]],
                         ids=["real", "check", "check-diff", "tags", "check-diff-tags"])
def test_each_run_mode_logs_in_with_the_rendered_key_and_removes_it(lab, args):
    """In a real run, a dry run (`--check`, `--check --diff`), a `--tags` run and a dry run under `--tags`: the host's
    connection is given the rendered key file, named by the host's cred key (`dev_1.key` for `dev-1`) in the run's own
    key dir (a dry run's is the real one's with `.check`), which exists, is 0600 in a 0700 dir and holds the SOPS
    domain's key, so the stand-in logs in and the ping answers (no host unreachable, failed or ignored: the play ignores
    an unreachable host, which the recap counts `ignored`), the run passes, its key dir (with the stale key an earlier
    run left) is gone after it, the other mode's dir still holds its stale key, and no line of the key is in the
    output. Guards I-87 (the render and the cleanup skipped in a dry run: no key, `Permission denied (publickey)`, an
    ignored unreachable host, rc 0, the stale key kept), I-93 (both skipped in a `--tags` run), a dry run that removes
    the keys of a real run it overlaps or logs in with them (the I-87 reads: one dir for both modes), a key file named
    by the host's name where its connection names its cred key's, and the render's `no_log` under `--diff`."""
    own, other = _dirs(lab, args)
    rc, out, recap, seen = _run(lab, *args)
    assert rc == 0, out
    assert recap == {"unreachable": 0, "failed": 0, "ignored": 0}, (recap, out)
    assert "ok: [%s]" % HOST in out.split("TASK [%s]" % PING, 1)[1].split("PLAY ", 1)[0], out
    assert "UNREACHABLE!" not in out, out
    key_file = os.path.join(own, "%s.key" % CRED)
    calls = [s for s in seen if s["identity"] == key_file]
    assert calls and all(s["exists"] for s in calls), seen
    want = hashlib.sha256(KEY.encode()).hexdigest()
    assert all(s["mode"] == 0o600 and s["dir_mode"] == 0o700 and s["sha256"] == want for s in calls), seen
    assert not os.path.exists(own), os.listdir(own)
    assert os.listdir(other) == ["stale.key"], os.listdir(other)
    assert all(line not in out for line in KEY.splitlines()), out


@pytest.mark.parametrize("args", [[], ["--check"]], ids=["real", "check"])
def test_a_key_dir_named_with_a_trailing_slash_is_the_same_dir(lab, args):
    """A `KONTROLL_SSH_KEY_DIR` given with a trailing `/` names the same dir: a real run's key file is in it, and a dry
    run's in that dir with `.check` appended (beside it), each logs in with it and removes its own dir, and the other
    mode's is left as it was. Guards group_vars/all.yml's strip of a trailing `/`: without it a dry run's dir is a
    hidden `.check` inside the real run's (which a real run's key-dir guard would refuse while it is there), and a real
    run's key file is named with `//`."""
    own, other = _dirs(lab, args)
    rc, out, recap, seen = _run(lab, *args, key_dir=lab.keys + "/")
    assert rc == 0 and recap == {"unreachable": 0, "failed": 0, "ignored": 0}, (recap, out)
    key_file = os.path.join(own, "%s.key" % CRED)
    assert seen and all(s["identity"] == key_file and s["exists"] for s in seen), seen
    assert not os.path.exists(own) and os.listdir(other) == ["stale.key"], out


@pytest.mark.parametrize("args", [[], ["--check"]], ids=["real", "check"])
def test_a_first_run_makes_the_key_dir_and_removes_it(lab, args):
    """On a controller no earlier run has left a key dir on, a real run and a dry run (`--check`) alike make the dir
    (0700; a dry run's own, with `.check`) and render the key in it, so the host's connection logs in with it, and the
    dir is gone after. The other rows start from a dir an earlier run left, so they cannot tell a dry run that skips
    the dir's task: the copy into a dir that does not exist fails the host, and a first dry run on a fresh box (the
    mandated one before a first apply) reaches no key-auth device (I-87; the dir task's `check_mode: false` dropped
    survived every other real-run row)."""
    own, _other = _dirs(lab, args)
    shutil.rmtree(own)
    rc, out, recap, seen = _run(lab, *args)
    assert rc == 0 and recap == {"unreachable": 0, "failed": 0, "ignored": 0}, (recap, out)
    key_file = os.path.join(own, "%s.key" % CRED)
    calls = [s for s in seen if s["identity"] == key_file]
    assert calls and all(s["exists"] and s["mode"] == 0o600 and s["dir_mode"] == 0o700 for s in calls), seen
    assert not os.path.exists(own), os.listdir(own)


@pytest.mark.parametrize("args", [[], ["--check"]], ids=["real", "check"])
def test_a_run_whose_key_cannot_be_decrypted_makes_no_key_dir(lab, args):
    """A real run and a dry run whose age identity is not the domain's recipient (as on a runner that cannot read the
    host's SOPS domain) fail the key-auth host LOUDLY at the render's decrypt assert, never log it in, end rc 2, and
    leave no key dir: the render makes the dir after that check. Guards the I-87 reads' N2: the dir was made first, so
    a run whose every key-auth host failed the check (the cleanup unreached) left an empty 0700 dir, in a dry run too
    once the dir task ran in one."""
    x25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.x25519")
    serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    raw = x25519.X25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    other_identity = os.path.join(lab.base, "other-age-identity.txt")
    with open(other_identity, "w", encoding="utf-8") as fh:
        fh.write(_age_recipient_module().bech32("age-secret-key-", raw).upper() + "\n")
    os.chmod(other_identity, 0o600)
    own, other = _dirs(lab, args)
    shutil.rmtree(own)
    rc, out, recap, seen = _run(lab, *args, env_add={"SOPS_AGE_KEY_FILE": other_identity})
    assert rc == 2 and recap["failed"] == 1, (recap, out)
    assert "could NOT be decrypted in this runner context" in out, out
    assert seen == [] and not os.path.exists(own), (seen, out)
    assert os.listdir(other) == ["stale.key"], os.listdir(other)


def test_a_run_that_skips_the_always_tag_renders_and_removes_no_key(lab):
    """A run under `--skip-tags always` drops the render and the cleanup, as it drops the run-id stamp: the host's
    connection names a key file that does not exist, so the stand-in refuses it and the host is unreachable, ignored by
    the play (the recap counts it `ignored`; rc 0), and the key dirs an earlier run left stay. Untagged, the plays ran
    under it; tagged `always` (I-93), they are dropped with the stamp. Holds that residual, which the plays' headers and
    SECURITY.md name, so a change that renders under it (or stops ignoring the host) is seen."""
    rc, out, recap, seen = _run(lab, "--skip-tags", "always")
    assert rc == 0 and recap == {"unreachable": 0, "failed": 0, "ignored": 1}, (recap, out)
    assert "Permission denied (publickey)" in out, out
    assert seen and not any(s["exists"] for s in seen), seen
    assert os.listdir(lab.keys) == ["stale.key"] and os.listdir(lab.check_keys) == ["stale.key"], out


# How a key dir the key plays must refuse is made, and the words the refusal gives for it.
SHAPES = {
    "other-file": "holds more than SSH keys: notes.txt",            # a regular file of another name
    "hidden": "holds more than SSH keys: .notes",                    # one `find` lists only with `hidden: true`
    "dir-named-key": "holds more than SSH keys: sub.key",            # a dir with a key's name: not a regular file
    "link-named-key": "holds more than SSH keys: link.key",          # a link with a key's name, to a file elsewhere
    "file": "is not a directory",                                    # the path is a file
    "link-dir": "is not a directory",                                # the path is a link to a dir that holds keys only
}


def _snapshot(path):
    """What is at `path`, a link not followed: its kind and mode, and for a dir each entry's (its bytes, or a link's
    target)."""
    def one(p):
        st = os.lstat(p)
        kind = "link" if stat.S_ISLNK(st.st_mode) else "dir" if stat.S_ISDIR(st.st_mode) else "file"
        if kind == "file":
            with open(p, "rb") as fh:
                return kind, stat.S_IMODE(st.st_mode), fh.read()
        return kind, stat.S_IMODE(st.st_mode), os.readlink(p) if kind == "link" else None
    held = {".": one(path)}
    if held["."][0] == "dir":
        held.update((name, one(os.path.join(path, name))) for name in os.listdir(path))
    return held


def _make_foreign(lab, shape, path):
    """Make the key dir at `path` (which holds a stale key) into one of SHAPES; return a snapshot of it and of the dir a
    link-dir names, to compare after the run."""
    elsewhere = os.path.join(lab.base, "elsewhere")
    os.makedirs(elsewhere)
    with open(os.path.join(elsewhere, "notes.txt"), "w", encoding="utf-8") as fh:
        fh.write("an operator's file the key plays must not touch\n")
    if shape in ("file", "link-dir"):
        shutil.move(path, os.path.join(elsewhere, "keys-only"))   # a dir that holds only a key, kept elsewhere
        if shape == "file":
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("an operator's file, at the key dir's path\n")
        else:
            os.symlink(os.path.join(elsewhere, "keys-only"), path)
    else:
        os.chmod(path, 0o755)   # an operator's dir, which a render that reached its dir task would chmod 0700
        if shape == "other-file":
            with open(os.path.join(path, "notes.txt"), "w", encoding="utf-8") as fh:
                fh.write("an operator's file\n")
        elif shape == "hidden":
            with open(os.path.join(path, ".notes"), "w", encoding="utf-8") as fh:
                fh.write("an operator's hidden file\n")
        elif shape == "dir-named-key":
            os.mkdir(os.path.join(path, "sub.key"))
        else:
            os.symlink(os.path.join(elsewhere, "notes.txt"), os.path.join(path, "link.key"))
    return _snapshot(path), _snapshot(elsewhere), _snapshot(os.path.join(elsewhere, "keys-only")) \
        if shape in ("file", "link-dir") else None


def _refused(out, own, shape):
    """The run's one run of the key-dir guard failed the host by name, naming the path and what is wrong with it."""
    assert out.count("TASK [%s]" % GUARD) == 1, out
    section = out.split("TASK [%s]" % GUARD, 1)[1].split("TASK [", 1)[0].split("PLAY ", 1)[0]
    assert "fatal: [%s" % HOST in section, section
    assert "kontroll_device_key_dir (%s) %s. The key plays write only" % (own, SHAPES[shape]) in section, section


@pytest.mark.parametrize("shape,args", [(s, []) for s in SHAPES] + [("other-file", ["--check"]), ("file", ["--check"])],
                         ids=["%s-real" % s for s in SHAPES] + ["other-file-check", "file-check"])
def test_the_render_refuses_a_key_dir_that_holds_anything_but_keys_and_keeps_it(lab, shape, args):
    """A run whose key dir (a dry run's own, `.check`) is not a directory (a file; a link, even to a dir that holds
    only keys) or holds anything but regular `*.key` files (a file of another name, a hidden one, a dir or a link named
    `*.key`) fails the key-auth host at the render's key-dir guard, by name, the message naming the path and what is
    wrong (the foreign entries alone, not the stale key beside them), before any write: the host never logs in, the run
    fails (rc 2), and the path, what it holds, its mode and what a link names are as they were; the other mode's dir is
    untouched. Guards I-95: the cleanup removes the key dir whole, so a `KONTROLL_SSH_KEY_DIR` naming an operator's
    dir (a home dir, `~/.ssh`, `/tmp`) was chmodded 0700 by the render and removed, with all it held, by the cleanup, in
    a real run and, since I-87's first commit, a dry one (the I-87 reads, S1); and each half of the guard: `hidden`,
    the regular-file test, the name test, the link not followed."""
    own, other = _dirs(lab, args)
    before = _make_foreign(lab, shape, own)
    rc, out, recap, seen = _run(lab, *args)
    assert rc == 2 and recap["failed"] == 1, (recap, out)
    _refused(out, own, shape)
    assert seen == [], seen
    elsewhere = os.path.join(lab.base, "elsewhere")
    after = _snapshot(own), _snapshot(elsewhere), _snapshot(os.path.join(elsewhere, "keys-only")) \
        if shape in ("file", "link-dir") else None
    assert after == before, (before, after)
    assert os.listdir(other) == ["stale.key"], os.listdir(other)


@pytest.mark.parametrize("shape,args", [(s, []) for s in SHAPES] + [("other-file", ["--check"])],
                         ids=["%s-real" % s for s in SHAPES] + ["other-file-check"])
def test_the_cleanup_refuses_a_key_dir_that_holds_anything_but_keys_and_keeps_it(lab, shape, args):
    """The cleanup run alone (`_cleanup-ssh-keys.yml`, as a run reaches it with the render's guard passed or its
    key-auth hosts gone) over a key dir of each refused shape: its key-dir guard fails the host, by name, the message
    naming the path and what is wrong, its delete never runs, the run fails (rc 2), and the path and all it holds are
    as they were. Guards I-95 in the cleanup itself, whose delete is the one that removed an operator's dir with all it
    held (a password-only fleet never runs the render's guard), and the guard imported before the delete, not after."""
    own, other = _dirs(lab, args)
    before = _make_foreign(lab, shape, own)
    rc, out, recap, seen = _run(lab, *args, playbook="zz-cleanup-only.yml")
    assert rc == 2 and recap["failed"] == 1, (recap, out)
    _refused(out, own, shape)
    assert "TASK [Delete the materialized per-host SSH keys (ephemeral)]" not in out, out
    elsewhere = os.path.join(lab.base, "elsewhere")
    after = _snapshot(own), _snapshot(elsewhere), _snapshot(os.path.join(elsewhere, "keys-only")) \
        if shape in ("file", "link-dir") else None
    assert after == before, (before, after)
    assert os.listdir(other) == ["stale.key"], os.listdir(other)
