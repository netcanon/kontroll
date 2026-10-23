# Custom Vector image = the stock pinned image + openssh-client — the ONE thing the file_tail_ssh logging
# method needs that the base image lacks. file_tail_ssh's Vector `exec` source runs `ssh <opts> root@<host>` to
# PULL a remote host's journald (the host forces `journalctl -f -o json` via a read-only forced-command key,
# roles/logging_file_tail_ssh). The stock timberio/vector:*-debian ships `journalctl` but NOT an ssh client
# (bench-verified 2026-06-17: `ssh: MISSING`), so an exec-ssh source on the stock image fails "ssh: not found".
#
# GATED — deploy-stack.yml builds this image + points KONTROLL_VECTOR_IMAGE at it ONLY when a class declares
# file_tail_ssh (docker/vector/generated/file_tail_ssh_*.generated.yaml exists). Every OTHER instance keeps the
# stock pinned image via vector.yaml's `${KONTROLL_VECTOR_IMAGE:-timberio/vector:0.56.0-debian@sha256:…}` default —
# Vector is NOT locally-built unless this method is actually in use (provision-only-what's-declared, like the S10
# journald receiver). Built by ansible/playbooks/deploy-stack.yml. Build context = this dir (the Dockerfile only).
#
# The FROM (tag and digest) is pinned in LOCKSTEP with docker/services/vector.yaml's default image — a drift-pin test
# (tests/unit/test_file_tail_ssh.py::test_dockerfile_from_is_pinned_in_lockstep_with_vector_yaml_default) fails if
# they diverge, so a base-image bump can't silently desync them.
FROM timberio/vector:0.56.0-debian@sha256:93b072b416fd29152f1bfe5bd2925a0b48999aeb069f3ae000691f82a135c200

# openssh-client only (no server); --no-install-recommends keeps the layer thin (no X11/krb extras). The exec
# source uses ssh with an explicit `-i` key + a UserKnownHostsFile, so no /root/.ssh provisioning is needed in the
# image. NON-ROOT runtime (C12 promotion fix): we do NOT re-assert `USER root` — the compose `user:` key
# (docker/services/vector.yaml) drives the non-root uid for BOTH this gated image AND the stock image, the one
# universal switch. The inherited ENTRYPOINT/CMD are unchanged (compose supplies the vector args).
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client \
 && rm -rf /var/lib/apt/lists/*
