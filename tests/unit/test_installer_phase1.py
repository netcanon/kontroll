"""Phase-1 proof obligations for the compose-native installer (docs/reviews/2026-06-29-compose-native-install/).

WHY — the installer relocates the install COMPUTE into an ephemeral container that bind-mounts the host's crown
jewels and drives the host docker socket. That is host-root-equivalent for the run's duration, so the security
floor (synthesis §5) is load-bearing and must be pinned in CI, not just reviewed once: no secret may ever reach an
image layer or a named volume; the installer must stay ephemeral + portless; the age key is referenced by FILE
(SOPS_AGE_KEY_FILE) never the literal value; no Dockerfile mints a key at build (MF-3); the operator-identity
parameterization (MF-1) and the host-task in-container guards (MF-6) must be present so a non-root operator's
`compose up` works and an in-container run doesn't crash on host-only tasks. Each test names the failure it guards.
"""
import os
import re

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

INSTALLER_SVC = "docker/services/installer.yaml"
INSTALLER_DOCKERFILE = "docker/installer/Dockerfile"
ENTRYPOINT = "docker/installer/entrypoint.sh"
DOCKERIGNORE = ".dockerignore"
INSTALL_PLAYBOOKS = [
    "ansible/playbooks/bootstrap.yml",
    "ansible/playbooks/local-canonical.yml",
    "ansible/playbooks/deploy-stack.yml",
    "ansible/playbooks/_log-run-id.yml",
]


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _active_lines(rel):
    """Non-blank, non-comment lines — so an assertion checks the artifact's BEHAVIOUR, not its explanatory prose."""
    return [ln.strip() for ln in _read(rel).splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _installer_service():
    """The parsed `init` service from the installer compose fragment."""
    doc = yaml.safe_load(_read(INSTALLER_SVC))
    return doc["services"]["init"]


def test_installer_secrets_are_host_binds_never_named_volumes():
    """Every installer mount MUST be a host BIND (path:path), never a named volume — a named volume is
    docker-socket-readable, i.e. strictly WIDER than a 0600 host file, so the age key / canonical / .env would be
    reachable by any container the socket can spawn (synthesis §5.1, the adversary's load-bearing rule). Guards a
    regression to a `name:/path` mount or a top-level `volumes:` declaration creeping into the installer."""
    doc = yaml.safe_load(_read(INSTALLER_SVC))
    assert "volumes" not in doc, "the installer fragment must declare NO named volumes (top-level volumes: key)"
    for vol in _installer_service().get("volumes", []):
        assert isinstance(vol, str), "use the short bind syntax so the host-path source is explicit"
        assert re.match(r"^(/|\$\{|\.)", vol), f"installer mount {vol!r} must be a host bind (path source), not a named volume"


def test_installer_is_ephemeral_and_portless():
    """The installer MUST be ephemeral (no `restart:`) and PORTLESS (no `ports:`) — the :rw docker socket makes it
    host-root for the run's duration ONLY; a restart policy or a published port would turn a one-shot installer into
    a standing attack surface (synthesis §5.5). network_mode: host is how it reaches localhost:3001 + the daemon."""
    svc = _installer_service()
    assert "ports" not in svc, "the installer must publish NO ports (portless — synthesis §5.5)"
    assert "restart" not in svc, "the installer must have NO restart policy (ephemeral one-shot — synthesis §5.5)"
    assert svc.get("network_mode") == "host", "the installer uses network_mode: host (localhost:3001 + host daemon)"


def test_installer_uses_sops_age_key_file_not_literal_value():
    """The age key MUST be referenced by FILE path (SOPS_AGE_KEY_FILE), never the literal SOPS_AGE_KEY env (which
    puts the private key value in `docker inspect` / the process environment — synthesis §5.4). Guards every
    installer artifact against the literal-value form."""
    for rel in (INSTALLER_SVC, INSTALLER_DOCKERFILE, ENTRYPOINT):
        # An env ASSIGNMENT of the literal key (SOPS_AGE_KEY: / SOPS_AGE_KEY=) not followed by _FILE — prose mentions
        # of "the literal SOPS_AGE_KEY env" (no : or =) are fine.
        for line in _active_lines(rel):
            assert not re.search(r"SOPS_AGE_KEY(?!_FILE)\s*[:=]", line), \
                f"{rel} must use SOPS_AGE_KEY_FILE (the file path), never the literal SOPS_AGE_KEY value env"


def test_no_dockerfile_mints_a_key_at_build():
    """No Dockerfile may run age-keygen or ssh-keygen at BUILD (MF-3 — no keygen container). A key minted in a
    build layer would be baked into the image (every puller gets the same 'secret'); keys are HOST gestures
    (bootstrap/fresh-init mint them through a runtime bind). Guards every Dockerfile in the tree."""
    for name in os.listdir(os.path.join(ROOT, "docker")):
        df_rel = f"docker/{name}/Dockerfile"
        if os.path.isfile(os.path.join(ROOT, df_rel)):
            active = " ".join(_active_lines(df_rel))   # ignore comment mentions (e.g. "age-keygen" describing `age`)
            assert "age-keygen" not in active, f"{df_rel} must not age-keygen at build (MF-3)"
            assert "ssh-keygen" not in active, f"{df_rel} must not ssh-keygen at build (MF-3)"


def test_installer_dockerfile_bakes_no_instance_or_secret():
    """The installer image MUST bake NO secret: no COPY/ADD of instance/, secrets, or a key file (synthesis §4.1).
    The crown jewels are runtime host binds. Guards a `COPY instance …` / `ADD …secrets…` creeping in (the repo is
    COPY'd wholesale, so this pairs with the .dockerignore test below as defence in depth)."""
    txt = _read(INSTALLER_DOCKERFILE)
    for bad in (r"COPY\s+[^\n]*instance", r"ADD\s+[^\n]*instance",
                r"COPY\s+[^\n]*secrets", r"COPY\s+[^\n]*\.key"):
        assert not re.search(bad, txt), f"installer Dockerfile must not bake {bad!r} (no secret in an image layer)"


def test_dockerignore_excludes_instance_keys_but_keeps_the_lockfile():
    """The repo-root .dockerignore MUST keep the private instance/ overlay, local/ scratch, age keys, and rendered
    .env OUT of every build context (the installer does COPY . /repo) — the actual mechanism that prevents a secret
    bake. It MUST NOT exclude ansible/collections/*.generated.yml: the Semaphore runner image bakes the fleet
    collections from that gitignored lockfile (docker/semaphore-runner/Dockerfile). Guards both failure directions."""
    active = _active_lines(DOCKERIGNORE)
    for needed in ("instance/", "local/", "keys.txt", "*.agekey"):
        assert needed in active, f".dockerignore must exclude {needed}"
    assert not any("generated" in a for a in active), \
        ".dockerignore must NOT exclude the generated lockfile — the runner image bakes collections from it"


def test_install_playbook_collections_are_within_the_control_plane_base():
    """The installer image bakes ONLY the CONTROL-PLANE collection base — modules/_core.yml, derived, never a
    hardcoded list. The install-path playbooks must therefore import NO collection outside that file, or
    `check`/`init` would fail 'couldn't resolve' (the PR #87 bug class). The base is `_core.yml` ALONE: a
    collection that only a device class declares is installed on a host-direct node only when the fleet enables that
    class — `community.docker` used to come from `docker_host`, so `deploy-stack` could not run on a fleet without
    Docker hosts, and the installer image hid it behind a fabricated one-module fleet. Guards a new collection
    sneaking into bootstrap/local-canonical/deploy-stack without being core. (Device collections bake into the
    runner at deploy time, not here.)"""
    core = yaml.safe_load(_read("modules/_core.yml")).get("collections") or []
    base = {c["name"] for c in core}
    # Collections actually INVOKED by the install playbooks: a module key (`^  community.x.y:`) or a lookup
    # (`lookup('community.x.y'`) — NOT comment mentions. ansible.builtin needs no collection install.
    usage = re.compile(r"""(?:^[ \t]*|lookup\(\s*['"])((?:community|cisco|fortinet|arista|ansibleguy)\.[a-z0-9_]+)\.[a-z0-9_]+""", re.M)
    used = set()
    for pb in INSTALL_PLAYBOOKS:
        used |= set(usage.findall(_read(pb)))
    missing = used - base
    assert not missing, f"install playbooks import {missing} which the baked control-plane base does not provide"


def test_mf1_env_render_owner_is_operator_identity_override():
    """deploy-stack's docker/.env render MUST chown to the operator's NUMERIC uid/gid via KONTROLL_OPERATOR_{UID,GID}
    (env-overridable, fact-free) at mode 0600, and emit KONTROLL_OPERATOR_HOME from the override (MF-1). Without it
    the play — which runs as root in-container — renders a root-owned .env that a non-root `compose up` cannot read
    (hard fail), and the canonical is chowned to root:1001 (the operator's `git push local` breaks)."""
    ds = _read("ansible/playbooks/deploy-stack.yml")
    assert re.search(r"owner:\s*\"\{\{\s*lookup\('env',\s*'KONTROLL_OPERATOR_UID'\)", ds), \
        ".env owner must come from KONTROLL_OPERATOR_UID (MF-1)"
    assert re.search(r"group:\s*\"\{\{\s*lookup\('env',\s*'KONTROLL_OPERATOR_GID'\)", ds), \
        ".env group must come from KONTROLL_OPERATOR_GID (MF-1)"
    assert "KONTROLL_OPERATOR_HOME={{ lookup('env', 'KONTROLL_OPERATOR_HOME')" in ds, \
        "KONTROLL_OPERATOR_HOME must come from the env override (MF-1)"
    lc = _read("ansible/playbooks/local-canonical.yml")
    assert "operator_uid" in lc and "KONTROLL_OPERATOR_UID" in lc, "local-canonical must chown the canonical by operator_uid (MF-1)"


def test_mf6_host_only_tasks_are_guarded_in_container():
    """The install playbooks must SKIP their host-only tasks in-container (MF-6) or `init` crashes: deploy-stack
    would `apt install docker` + start a systemd service (no systemd in a container); bootstrap would install
    qemu-guest-agent's unit; local-canonical would write a useless container-local gid-1001 group. Each must gate on
    KONTROLL_IN_CONTAINER. Guards the in-container run against these host-setup tasks."""
    for pb in ("ansible/playbooks/bootstrap.yml", "ansible/playbooks/deploy-stack.yml", "ansible/playbooks/local-canonical.yml"):
        txt = _read(pb)
        assert "KONTROLL_IN_CONTAINER" in txt, f"{pb} must derive in_container from KONTROLL_IN_CONTAINER (MF-6)"
        assert re.search(r"when:\s*(not\s+)?in_container", txt), f"{pb} must guard a host-only task on in_container (MF-6)"


def test_installer_resolves_the_baked_collections_at_runtime():
    """The installer MUST set ANSIBLE_COLLECTIONS_PATH to the baked collection dir (/usr/share/ansible/collections)
    — ansible/ansible.cfg pins a RELATIVE `collections_path = collections` (→ /repo/ansible/collections, which the
    host /repo bind shadows EMPTY at runtime), so without the env override the baked control-plane collections (and
    bootstrap's run-time fleet install, which also lands there) are unresolvable. Dogfood-caught 2026-06-29: `check`
    failed 'couldn't resolve community.docker.docker_image_build'. The Dockerfile bakes to the SAME path."""
    env = _installer_service().get("environment", {})
    assert env.get("ANSIBLE_COLLECTIONS_PATH") == "/usr/share/ansible/collections", \
        "installer must set ANSIBLE_COLLECTIONS_PATH to the baked dir (ansible.cfg's relative collections_path shadows it)"
    df = _read(INSTALLER_DOCKERFILE)
    assert "KONTROLL_COLLECTIONS_PATH=/usr/share/ansible/collections" in df, \
        "the Dockerfile must bake collections to the SAME path the runtime env resolves"


def test_installer_storage_bind_is_the_bare_root_not_a_secret_subdir():
    """The installer binds the bare storage ROOT (/var/lib/kontroll) — it is the PROVISIONER that CREATES the
    uid-scoped tree deploy-stack populates, so the storage-chown guard exempts exactly that bind. This pins the
    exemption NARROW: the installer must bind only the bare root, never a secret subdir (loki/backups/api/audit/
    onboard-gui) — which would smuggle a secret store past the G9 consumer-bind guard via the installer's exemption."""
    storage_binds = [v for v in _installer_service().get("volumes", []) if "/var/lib/kontroll" in v or "KONTROLL_STORAGE_ROOT" in v]
    assert storage_binds, "the installer must bind the storage root (it provisions the tree)"
    for v in storage_binds:
        src = v.split(":")[-2] if v.count(":") >= 2 else v.split(":")[0]
        assert not re.search(r"/var/lib/kontroll/\S|KONTROLL_STORAGE_ROOT[:}-]+[^}]*}/\S", v), \
            f"installer storage bind {v!r} must be the bare root, not a secret subdir (keeps the guard exemption narrow)"


def test_entrypoint_dispatches_the_documented_verbs():
    """The entrypoint must dispatch exactly the documented verbs (init/check/fresh-init/configure-semaphore/shell)
    plus an ansible-playbook passthrough — the thin-dispatcher contract (synthesis §4.2). `check` must be the
    mandated --check --diff dry-run. Guards a verb being dropped or `check` losing its dry-run flags."""
    ep = _read(ENTRYPOINT)
    for verb in ("init)", "check)", "fresh-init)", "configure-semaphore)", "shell)"):
        assert verb in ep, f"entrypoint must dispatch the {verb[:-1]} verb"
    assert "--check --diff" in ep, "the `check` verb must run deploy-stack with --check --diff (the mandated dry-run)"
    assert re.search(r"exec\s+ansible-playbook\s+\"\$verb\"", ep), "entrypoint must passthrough to ansible-playbook"


def test_fresh_init_hands_the_scaffolded_instance_to_the_operator():
    """`fresh-init` runs as root inside the installer container and writes `instance/` through a host bind, so its
    output landed on the host owned by root — the very files the next step tells the operator to edit. On a fresh
    VM `$EDITOR instance/fleet.yml` failed with permission denied until they worked out it needed sudo
    (live-caught 2026-07-28). MF-1 already injects the operator identity for exactly this class of problem; it
    just never covered these artifacts. Guards the chown, and that the verb is NOT `exec`'d away (exec replaces
    the shell, so nothing could run after it) while the scaffold's own exit code survives."""
    block = _read(ENTRYPOINT).split("  fresh-init)", 1)[1].split("  configure-semaphore)", 1)[0]
    assert 'exec python3 "$REPO/scripts/kontroll-init.py"' not in block, \
        "exec replaces the shell — nothing can chown afterwards"
    assert "KONTROLL_OPERATOR_UID" in block, "the operator identity must be honoured"
    assert 'chown -R "$KONTROLL_OPERATOR_UID' in block and "$REPO/instance" in block
    assert "SOPS_AGE_KEY_FILE" in block, "the minted age key is root-owned too"
    assert "exit $rc" in block, "the scaffold's own exit code must survive the chown"


def test_installer_bakes_the_core_base_without_fabricating_a_fleet():
    """The installer Dockerfile derives its collection base with `gen-requirements.py --core` and writes no fleet
    file. Guards two failures: the fabricated `enabled_modules: [docker_host]` fleet coming back (a file written
    into the tree and deleted again to make a generator happy), and a build that calls the generator with no flag —
    which, with no `instance/` in the build context, falls through to the shipped EXAMPLE fleet and bakes device
    collections into an image that must carry none."""
    txt = _read(INSTALLER_DOCKERFILE)
    code = "\n".join(line for line in txt.splitlines() if not line.lstrip().startswith("#"))
    assert "scripts/gen-requirements.py --core" in code
    assert "enabled_modules" not in code and "config/fleet.yml" not in code
    assert len(re.findall(r"gen-requirements\.py", code)) == 1, "the generator runs once, with --core"
