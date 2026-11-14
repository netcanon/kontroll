"""Fresh-install (Debian 12) compatibility guards for the from-scratch flow (docs/SETUP.md).

WHY — a live dogfood of the documented install on a clean Debian 12 (bookworm) control node hit four ansible /
collection version breaks, each blocking deploy-stack or configure-semaphore. This file pins the two that live in
static script/config; the other two are guarded where their logic lives (test_install_collections.py
::version_satisfied — the dist-packages floor shadow; test_configure_semaphore.py::admin_password — the sops
self-decrypt):

- deploy-stack.yml uses `ansible.builtin.deb822_repository` (added in ansible-core 2.15), but Debian's apt `ansible`
  metapackage is core 2.14 → "couldn't resolve module/action". So install-prereqs.sh must install ansible-core
  (>=2.16) via pip and NOT apt the `ansible` metapackage (which ALSO preseeds stale community.* collections into
  dist-packages that shadow the project lockfile pins).
- ansible.cfg's old `stdout_callback = yaml` was the `community.general.yaml` alias, REMOVED in community.general
  12.0 → every ansible run aborts "community.general.yaml has been removed". The built-in `default` callback with
  its `result_format` set to yaml (`callback_result_format = yaml`, the key ansible reads) is the version-proof
  equivalent.
"""
import os
import re
import shutil
import subprocess

import pytest

from kontroll import paths

pytestmark = pytest.mark.unit


def _read(rel):
    with open(os.path.join(paths.ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def test_install_prereqs_installs_ansible_core_via_pip_not_apt():
    """install-prereqs.sh must pip-install ansible-core with a >=2.16 floor (deploy-stack's deb822_repository needs
    core >=2.15) and must NOT apt-install the bare `ansible` metapackage — apt's core 2.14 is too old AND its bundled
    community.* collections in dist-packages shadow the lockfile pins (a too-old community.docker masked the >=4.0.0
    pin → docker_image_build unresolved). Guards a regression back to the apt ansible that broke the fresh deploy."""
    sh = _read("scripts/install-prereqs.sh")
    assert re.search(r"pip[0-9]?\s+install[^\n]*ansible-core", sh), "ansible-core must be installed via pip"
    assert re.search(r"ansible-core>=2\.1[5-9]", sh), "ansible-core needs a >=2.16 floor (deb822_repository, core >=2.15)"
    assert not re.search(r"^\s+ansible\s+git\b", sh, re.M), \
        "apt must NOT install the bare `ansible` metapackage (core 2.14 + dist-packages collection shadow)"


def test_ansible_cfg_uses_builtin_default_callback_not_removed_yaml_alias():
    """ansible/ansible.cfg must use the built-in `default` stdout_callback with its result format yaml — set as
    `callback_result_format`, and where `ansible-config` is installed, ansible itself must say the default
    callback's `result_format` is yaml from that file — NOT the bare `yaml` alias (community.general.yaml, removed in
    community.general 12.0 → aborts every run). Guards the fresh-install break where a current community.general made
    `stdout_callback = yaml` fatal — and propagates to Semaphore, which runs plays against this same canonical
    ansible.cfg — and a key ansible never reads: a bare `result_format` was ignored, every result printed as JSON
    and the stop after a sync's listing one line of it (read A-f4, N2)."""
    cfg = _read("ansible/ansible.cfg")
    assert re.search(r"^\s*stdout_callback\s*=\s*default\s*$", cfg, re.M), "stdout_callback must be `default`"
    assert re.search(r"^\s*callback_result_format\s*=\s*yaml\s*$", cfg, re.M), "callback_result_format: yaml"
    assert not re.search(r"^\s*result_format\s*=", cfg, re.M), "a bare `result_format` is no key ansible reads"
    if shutil.which("ansible-config"):
        p = subprocess.run(["ansible-config", "dump", "--only-changed", "-t", "callback"], capture_output=True,
                           text=True, env=dict(os.environ, ANSIBLE_CONFIG=os.path.join(paths.ROOT, "ansible",
                                                                                          "ansible.cfg")))
        default = re.search(r"^default:\n_+\n((?:.+\n?)*)", p.stdout, re.M)     # the section, to its blank line
        assert p.returncode == 0 and default, p.stdout + p.stderr
        assert re.search(r"^result_format\(.*ansible\.cfg\) = yaml$", default.group(1), re.M), p.stdout
    assert not re.search(r"^\s*stdout_callback\s*=\s*yaml\s*$", cfg, re.M), \
        "the bare `yaml` alias (community.general.yaml) was removed in community.general 12.0 — do not reintroduce it"
