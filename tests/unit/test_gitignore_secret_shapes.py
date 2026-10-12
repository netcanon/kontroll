"""The ignore rules for decrypted copies, keys, backups and env files — and the two things they must NOT hide.

WHY: `.gitignore` is the quiet half of keeping plaintext out of the canonical (the loud, tested half is
scripts/lib/plaintext-guard.sh). A `sops -d … > network.dec.yml`, an editor's `.bak` of a file that was open
decrypted, a key saved as `something.key`, a copy of `docker/.env` taken before a re-render: each used to be
trackable, because the rules matched only a name that ENDS in `.dec` or `.decrypted`, two exact env-file names and
`*.agekey`. They are ignored now. Some names must stay trackable on purpose: `docker/.env.example` and any
`.env.<name>.example` (the committed templates — the `.env.*` rule would swallow them without its negations) and
a public CA certificate saved as `.pem` (instance.yml tells an operator to commit one), which no rule of the root
file may hide.

The check runs in a scratch repository that holds copies of the shipped root `.gitignore` and of the scaffolded
`instance/.gitignore` — the layout of an instance repository, where `instance/` is re-included. In THIS
repository everything under `instance/` is ignored anyway and the check would prove nothing.
"""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = [pytest.mark.unit,
              pytest.mark.usefixtures("isolated_git"),
              pytest.mark.skipif(not shutil.which("git"), reason="needs git on PATH")]

IGNORED = [
    "instance/secrets/network.dec.yml", "instance/secrets/network.decrypted.yml", "instance/secrets/network.plain",
    "instance/secrets/network.plain.json", "instance/secrets/network.plaintext.yml", "instance/secrets/network.dec",
    "instance/secrets/network.sops.yml.bak", "instance/secrets/network.sops.yml.orig",
    "instance/secrets/network.sops.yml~", "instance/secrets/age.key", "instance/secrets/keys.txt",
    "kontroll-breakglass.key", "breakglass.agekey", "id_rsa", "id_ed25519", "id_ecdsa", "instance/inventory/id_ed25519",
    # instance/certs/ and instance/actuation/ are re-included: a key-shaped NAME inside them must stay ignored
    "instance/certs/server.key", "instance/certs/ca-key.agekey", "instance/certs/id_ed25519", "instance/certs/keys.txt",
    "instance/actuation/demo/.env", "instance/actuation/demo/unit.yml.bak",
    ".env", ".env.local", ".env.bak", "docker/.env", "docker/.env.bak", "docker/.env.homepage",
    "docker/.env.homepage.bak", "docker/.env.example.bak", "notes.txt~",
]
TRACKABLE = [
    "docker/.env.example",                    # the committed template
    "docker/.env.homepage.example",           # ...and a template for one of the other env files
    "instance/trust/keys/pve-ca.pem",         # a PUBLIC certificate as `.pem`, in a directory that is re-included
    "instance/certs/pve-ca.pem",              # ...and where instance.yml tells the operator to put one
    "instance/certs/known_hosts", "instance/actuation/demo/unit.yml", "instance/actuation/demo/vars.yml",
    "instance/secrets/network.sops.yml",      # the encrypted domain itself
    "instance/secrets/README.md", "instance/fleet.yml", "instance/.sops.yaml",
    # NOT covered by a name rule, on purpose — nothing about the name says "secret"; the guard refuses it by rule
    "instance/secrets/network.yml",
]


@pytest.fixture(scope="module")
def instance_repo(tmp_path_factory):
    tree = tmp_path_factory.mktemp("instance-repo")
    shutil.copyfile(os.path.join(ROOT, ".gitignore"), str(tree / ".gitignore"))
    (tree / "instance").mkdir()
    shutil.copyfile(os.path.join(ROOT, "instance.example", ".gitignore"), str(tree / "instance" / ".gitignore"))
    subprocess.run(["git", "-C", str(tree), "init", "-q"], check=True, capture_output=True)
    return tree


def _ignored(tree, path):
    return subprocess.run(["git", "-C", str(tree), "check-ignore", "-q", "--no-index", path],
                          capture_output=True).returncode == 0


@pytest.mark.parametrize("path", IGNORED)
def test_decrypted_copies_keys_backups_and_env_files_are_ignored(instance_repo, path):
    """Each name a decrypted copy, a private key, an editor backup or an env file is commonly saved under is
    ignored in an instance repository — also inside the re-included `instance/` directories. Guards a later tidy
    of `.gitignore` quietly making one of them trackable again: `git add -A` would then stage it, and only the
    plaintext guard would stand between it and the canonical."""
    assert _ignored(instance_repo, path), "%s is trackable" % path


@pytest.mark.parametrize("path", TRACKABLE)
def test_the_rules_do_not_hide_what_must_be_committed(instance_repo, path):
    """The env template, a public CA certificate saved as `.pem`, an encrypted secrets file and the overlay's own
    files stay trackable. Guards the ignore rules being widened one pattern too far — `.env.*` without its
    negation hides the template every install copies, and a `*.pem` rule would hide the certificate that
    instance.yml tells the operator to commit, so the runner's clone would lack it with no error anywhere."""
    assert not _ignored(instance_repo, path), "%s is ignored" % path
