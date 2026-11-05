#!/usr/bin/env bash
# fresh-node-check.sh — the EVALUATING layer (L2): a brand-new node's install, run in check mode, from a clean
# export of HEAD. Every other gate reads the playbooks as text or parses them (`--syntax-check`); this one makes
# Ansible evaluate them — templating, the variable chain, `set_fact`, the dispatch, the summaries — against the
# instance a fresh `kontroll-init --fresh` writes, so a play that only fails on a node nobody has configured yet
# (the 2026-07-28 rebuild found four) fails here first.
#
# What it runs, each in a temporary directory it removes on exit:
#   1. `git archive HEAD` into it, made a repository on `main` with HEAD's tree as its one commit: a tracked tree
#      with no instance/, in a worktree on `main`, as a fresh clone has.
#   2. kontroll-init --fresh, with HOME and SOPS_AGE_KEY_FILE inside that directory: a throwaway control key and
#      the bootstrap secrets, minted by the real code.
#   3. deploy-stack.yml --check --diff, with KONTROLL_IN_CONTAINER=1 as the installer's `check` verb runs it
#      (docker/installer/entrypoint.sh), so the host-Docker play is skipped; local-canonical.yml, which it imports,
#      is evaluated too. It must pass.
#   4. The same with every service: it must FAIL, and fail for the one reason a fresh node must refuse — the
#      onboard-gui's age key is missing (G9; deploy-stack never mints it). The message is matched, not just the
#      exit, so a play that breaks for any other reason cannot pass this step.
#   5. bootstrap.yml --check, with KONTROLL_IN_CONTAINER=1 as the installer's `init` verb runs it (its compose
#      service sets it, docker/services/installer.yaml): the tasks that install the host's OS toolchain, which the
#      installer image bakes, are skipped. Those are the legacy host path's, and their check reads the host's own
#      packages (apt in check mode needs the host's python3-apt), not what a fresh node installs.
#   6. ping.yml and backup-configs.yml --check against tests/mock-inventory.yml (one fake host per shipped device
#      class, `ansible_connection: local`, RFC-5737 addresses): the fleet plays dispatch every class's `check` and
#      `backup` entrypoint and reach their summaries. No device is contacted and none answers: the step proves the
#      plays evaluate, not that a device does. ping.yml must pass (it rescues a host it cannot reach);
#      backup-configs.yml must fail, at exactly the classes whose capture reads a device or needs a credential, each
#      at that task (listed below).
#
# What it never does: write outside its temporary directory (HOME is moved there before kontroll-init runs, and the
# tree it runs is the export, never this checkout), start a container, or reach a device. It is run by CI's validate
# job, not by tests/validate.sh: validate.sh also runs on a configured control node, where scaffolding a second
# instance is wrong. Run it by hand from a checkout: `bash tests/fresh-node-check.sh` (needs ansible-playbook with
# the collections the modules name, sops, age-keygen and git, and for the deploy passes the `docker` Python library,
# the buildx plugin and a Docker daemon to read). KONTROLL_FRESH_KEEP=1 keeps the directory and prints its path.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
for tool in git ansible-playbook sops age-keygen python3; do
  command -v "$tool" >/dev/null || { echo "fresh-node-check: FAIL — $tool is not on PATH" >&2; exit 1; }
done

tmp=$(mktemp -d "${TMPDIR:-/tmp}/kontroll-fresh.XXXXXX")
if [ "${KONTROLL_FRESH_KEEP:-}" = 1 ]; then
  trap 'echo "fresh-node-check: kept $tmp"' EXIT
else
  trap 'rm -rf "$tmp"' EXIT
fi
mkdir -p "$tmp/tree" "$tmp/home" "$tmp/logs" "$tmp/backups"

# The collections the runner installed live under the CALLER's home; the export's ansible.cfg names its own
# (empty) collections dir and HOME moves below, so name both before it does.
export ANSIBLE_COLLECTIONS_PATH="${ANSIBLE_COLLECTIONS_PATH:-$HOME/.ansible/collections:/usr/share/ansible/collections}"
export HOME="$tmp/home"
export SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt"
unset KONTROLL_IN_CONTAINER

git -C "$here" archive HEAD | tar -x -C "$tmp/tree"
tree="$tmp/tree"
# A fresh node's tree is a clone: a repository on `main`, which local-canonical.yml's landing reads as a worktree
# (its plaintext guard refuses a tree git cannot read). The export is made one, HEAD's tree its one commit.
git -C "$tree" init -q -b main
git -C "$tree" add -A
git -C "$tree" -c user.name=fresh-node-check -c user.email=fresh-node-check@example.invalid commit -qm "HEAD's tree"

# step <name> <expect: pass|fail> <cmd…>: run it from the export's ansible/ dir into its own log; a pass that was
# expected to fail, or the reverse, prints the log's tail and exits 1.
step() {
  local name=$1 expect=$2 rc=0
  shift 2
  (cd "$tree/ansible" && "$@") > "$tmp/logs/$name.log" 2>&1 || rc=$?
  if { [ "$expect" = pass ] && [ "$rc" -eq 0 ]; } || { [ "$expect" = fail ] && [ "$rc" -ne 0 ]; }; then
    echo "fresh-node-check: $name — ok (rc=$rc, expected $expect)"
    return 0
  fi
  echo "fresh-node-check: FAIL — $name exited $rc, expected $expect; its log's last lines:" >&2
  tail -n 60 "$tmp/logs/$name.log" >&2
  exit 1
}

# needs <name> <ERE> <what>: the step's log must hold the pattern (what the step is for, not just its exit).
needs() {
  if grep -Eq -- "$2" "$tmp/logs/$1.log"; then
    echo "fresh-node-check: $1 — says $3"
    return 0
  fi
  echo "fresh-node-check: FAIL — $1's log does not say $3 (/$2/); its last lines:" >&2
  tail -n 60 "$tmp/logs/$1.log" >&2
  exit 1
}

# recap <name>: the step's log holds a PLAY RECAP and no host in it failed (a pass that skipped every play has none).
recap() {
  if grep -q '^PLAY RECAP' "$tmp/logs/$1.log" && ! grep -Eq ' failed=[1-9]' "$tmp/logs/$1.log"; then
    echo "fresh-node-check: $1 — its recap names no failed host"
    return 0
  fi
  echo "fresh-node-check: FAIL — $1's log has no PLAY RECAP, or a host in it failed; its last lines:" >&2
  tail -n 60 "$tmp/logs/$1.log" >&2
  exit 1
}

# stops <name> <host=task>…: the step's log has a PLAY RECAP, the hosts it names with a failure are exactly the
# listed ones, and each failed at the listed task (the task of its last failure line, `role : name` as Ansible prints
# it). A mismatch prints what the log holds.
stops() {
  local name=$1
  shift
  if python3 - "$tmp/logs/$name.log" "$@" <<'PY'
import re, sys
log, want = sys.argv[1], dict(a.split("=", 1) for a in sys.argv[2:])
text = open(log, encoding="utf-8", errors="replace").read()
task, last = None, {}
for line in text.split("\n"):
    m = re.match(r"TASK \[(.*)\] \*+$", line)
    if m:
        task = m.group(1)
    m = re.match(r"(?:fatal|failed): \[([^\]\s]+)", line)
    if m:
        last[m.group(1)] = task
recap = text.split("\nPLAY RECAP ", 1)[1] if "\nPLAY RECAP " in text else ""
failed = sorted(h for h, n in re.findall(r"^(\S+)\s+: ok=\d+ .*\bfailed=(\d+)", recap, re.M) if n != "0")
got = {h: last.get(h) for h in failed}
if recap and got == want:
    sys.exit(0)
print("the recap's failed hosts and where each failed: %s; listed: %s" % (got, want), file=sys.stderr)
sys.exit(1)
PY
  then
    echo "fresh-node-check: $name — the hosts that failed, and where, are the listed ones"
    return 0
  fi
  echo "fresh-node-check: FAIL — $name: the hosts that failed are not the listed ones, each at its task; its last lines:" >&2
  tail -n 60 "$tmp/logs/$name.log" >&2
  exit 1
}

step init pass python3 ../scripts/kontroll-init.py --fresh --mgmt-ip 192.0.2.10 --domain example.test \
  --trust-mode separated --no-break-glass
needs init '^==> kontroll-init done' "kontroll-init finished"
[ -f "$SOPS_AGE_KEY_FILE" ] && [ -f "$tree/instance/.sops.yaml" ] || {
  echo "fresh-node-check: FAIL — kontroll-init wrote no key at \$SOPS_AGE_KEY_FILE or no instance/.sops.yaml" >&2
  exit 1
}

step deploy-check pass env KONTROLL_IN_CONTAINER=1 ansible-playbook playbooks/deploy-stack.yml --check --diff
recap deploy-check

step deploy-check-all fail env KONTROLL_IN_CONTAINER=1 ansible-playbook playbooks/deploy-stack.yml --check --diff \
  -e '{"stack_services": ["semaphore", "homepage", "prometheus", "grafana", "loki", "vector", "api", "onboard-gui"]}'
needs deploy-check-all "onboard-gui's age key at .* is MISSING" "the onboard-gui's age key is missing (G9)"

step bootstrap-check pass env KONTROLL_IN_CONTAINER=1 ansible-playbook playbooks/bootstrap.yml --check
recap bootstrap-check

step ping-check pass ansible-playbook -i ../tests/mock-inventory.yml playbooks/ping.yml --check
recap ping-check
# The backup pass must FAIL, and only where the mock inventory stops it. ping.yml rescues a host its check cannot
# reach; backup-configs.yml must not (a capture that fails is a failed run), so under `ansible_connection: local` a
# class whose capture reads its device, or needs an API credential a fresh node does not have, fails at that task.
# Every other host must pass and the run must reach its summary. A host failing anywhere else, or a listed one
# passing or failing at another task, fails the check: a class added to the mock inventory whose backup reaches a
# device is listed here, at that task.
step backup-check fail ansible-playbook -i ../tests/mock-inventory.yml playbooks/backup-configs.yml --check \
  -e "config_backup_dir=$tmp/backups"
needs backup-check '^PLAY \[Backup run summary\]' "the run reached its summary"
stops backup-check \
  "mock-edge=opnsense : Read /conf/config.xml" \
  "mock-fortigate=backend_api : Refuse an unresolvable API credential (var NAMES only — never the value)" \
  "mock-switch=backend_netcommon_cli : Pull config via cli_command" \
  "mock-xe=backend_netcommon_cli : Pull config via cli_command"

echo "fresh-node-check: PASS"
