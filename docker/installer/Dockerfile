# docker/installer/Dockerfile — the EPHEMERAL kontroll installer image (compose-native install, Phase 1).
#
# WHY this exists: the install GLUE (a host ansible-core + the device collections + sops + age) is the
# deployability pain — a fresh control node needs an exact host toolchain, and a version skew there is the
# whole PR #87 fresh-Debian-12 bug class. This image relocates that toolchain OFF the host: the operator's
# node needs only Docker + git, and `docker compose run --rm init …` runs the UNCHANGED bootstrap /
# local-canonical / deploy-stack playbooks from inside this container against the host docker socket + an
# `instance/` bind. Design-of-record: docs/reviews/2026-06-29-compose-native-install/99-synthesis.md (Phase A).
#
# SECURITY (synthesis §4.1 / §5): this image bakes NO secret, NO age key, NO `instance/`, NO `instance/trust/`.
# The crown jewels (age key :ro, canonical, the rendered .env 0600) are HOST BIND files injected at run time by
# docker/services/installer.yaml — never an image layer, never a named volume. The repo-root .dockerignore is
# the guard that keeps instance/ + keys + local/ out of the build context (a Phase-1 test pins it).
#
# Build context = repo ROOT (same as docker/semaphore-runner/Dockerfile). The image is built LOCALLY in Phase 1
# (trusted at the level of today's host toolchain — NOT yet digest-pinned; that rigor is Phase C, synthesis §5.6).
FROM debian:12-slim

ENV DEBIAN_FRONTEND=noninteractive

# Base toolchain. git+tar: the in-container playbooks' git work (local canonical) + parity with the host floor.
# age: the secrets-at-rest keypair tool (age-keygen) — `fresh-init` mints the control key with it. openssl:
# token/cert generation. python3/pip/venv: ansible-core (below) + the kontroll service layer the playbooks call.
# ca-certificates/curl/gnupg: the Docker apt repo + the pinned sops release fetch.
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends \
      git tar curl ca-certificates gnupg openssl age sudo openssh-client \
      python3 python3-pip python3-venv \
 && rm -rf /var/lib/apt/lists/*
# openssh-client: ssh-keygen — bootstrap.yml mints the dedicated device-access key (~/.ssh/ansible_ed25519) with
# it (dogfood-caught 2026-06-29: "No such file or directory: b'ssh-keygen'").
# sudo: the playbooks use `become: true` throughout; ansible's default become method is sudo even when the
# connection user is already root, so the binary must be present (passwordless as root) or every become task errors.

# The Docker CLI + compose plugin ONLY — this container DRIVES the host daemon over the bound socket; it never
# runs a daemon of its own (no docker-ce/containerd). deploy-stack.yml shells `docker compose` + builds the
# runner/vector images through this CLI against the host socket. The keyring + deb822 .sources mirror
# scripts/install-prereqs.sh exactly (same Signed-By, no "Conflicting values" if the two ever co-exist).
RUN install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && chmod a+r /etc/apt/keyrings/docker.asc \
 && printf 'Types: deb\nURIs: https://download.docker.com/linux/debian\nSuites: bookworm\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' \
      "$(dpkg --print-architecture)" > /etc/apt/sources.list.d/docker.sources \
 && apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends \
      docker-ce-cli docker-buildx-plugin docker-compose-plugin \
 && rm -rf /var/lib/apt/lists/*

# sops is NOT in Debian apt — the pinned GitHub release, in lock-step with scripts/install-prereqs.sh +
# bootstrap.yml's sops_version. community.sops shells out to this binary to decrypt the SOPS service domains.
ARG SOPS_VERSION=v3.13.1
RUN curl -fsSL "https://github.com/getsops/sops/releases/download/${SOPS_VERSION}/sops-${SOPS_VERSION}.linux.$(dpkg --print-architecture)" \
      -o /usr/local/bin/sops \
 && chmod 0755 /usr/local/bin/sops

# ansible-core via pip, pinned to the SAME range install-prereqs.sh used on the host (>=2.16 for deploy-stack's
# ansible.builtin.deb822_repository). ansible-lint for the future `validate` verb (synthesis §4.1). The `docker`
# Python SDK is the RUNTIME dependency of community.docker's docker_network / docker_image_build modules deploy-stack
# uses (it pulls in requests; without it the play fails "No module named 'requests'" — dogfood-caught 2026-06-29).
# debian:12 python is externally-managed ⇒ --break-system-packages (single-purpose image, no other Python tenant).
RUN python3 -m pip install --break-system-packages --no-cache-dir "ansible-core>=2.16,<2.19" ansible-lint docker

# The repo (playbooks + scripts + modules). The .dockerignore (repo root) keeps instance/, secrets, keys, local/,
# .git, and docs/reviews OUT. NOTE: at run time docker/services/installer.yaml binds the host /repo over /repo,
# so this baked copy is only used HERE (to install the control-plane collections below); run time reads the bind.
WORKDIR /repo
COPY . /repo

# Bake the CONTROL-PLANE collection base via the SAME never-brick wrapper bootstrap uses (G-4 no-drift). The
# install playbooks (bootstrap/local-canonical/deploy-stack) import only collections listed in modules/_core.yml
# (tests/unit/test_installer_phase1.py holds that direction) — NOT the device-fleet collections (cisco.ios,
# fortinet.fortios, … bake into the Semaphore RUNNER image at deploy time, from the full lockfile bootstrap
# generates). The base is DERIVED from that file by the same generator (`--core`) — never a hardcoded list
# (CLAUDE.md hard rule) and no fleet: none exists on a FRESH node at image build, and none is fabricated.
# instance/trust/ is absent ⇒ the wrapper does a plain pin+checksum install (byte-identical to the runner image;
# L2b applies only on the control node at run time, where instance/trust/ is bind-mounted). The two generated
# files are removed in this one layer.
RUN set -eu; \
    python3 scripts/gen-requirements.py --core; \
    KONTROLL_COLLECTIONS_PATH=/usr/share/ansible/collections python3 scripts/install-collections.py; \
    rm -f ansible/collections/requirements.generated.yml ansible/collections/trust.generated.yml

# git's dubious-ownership guard: the run-time /repo bind is operator-owned but this container runs as root —
# trust any repo path (single-tenant, ephemeral). Mirrors the runner image.
RUN git config --system --add safe.directory '*'

# The installer image's marker. kontroll-init's in-container guard (scripts/kontroll-init.py, _key_made_away) reads
# it, and the dispatcher copied below, as well as KONTROLL_IN_CONTAINER, which a `sudo` or an `env -i` in the
# installer's shell drops: a missing control key file other than the mounted keys.txt would be made where the
# container loses it or leaves it in the checkout, and one of another name is one no installer verb reads
# (docs/troubleshooting.md §11).
RUN install -d -m 0755 /etc/kontroll && : > /etc/kontroll/installer-image

COPY docker/installer/entrypoint.sh /usr/local/bin/kontroll-installer
RUN chmod 0755 /usr/local/bin/kontroll-installer

ENTRYPOINT ["/usr/local/bin/kontroll-installer"]
CMD ["init"]
