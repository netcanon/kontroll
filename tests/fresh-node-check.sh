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
#   3. deploy-stack.yml --check --diff, with KONTROLL_IN_CONTAINER=1 and an operator named, as the installer's
#      `check` verb runs it (docker/installer/entrypoint.sh; its launcher always names the operator,
#      docker/services/installer.yaml), so the host-Docker play is skipped; local-canonical.yml, which it imports,
#      is evaluated too. It must pass and reach the stack play's last task.
#   4. The same with every service: it must FAIL, and fail for the one reason a fresh node must refuse — the
#      onboard-gui's age key is missing (G9; deploy-stack never mints it). The message is matched, not just the
#      exit, so a play that breaks for any other reason cannot pass this step.
#   5. The same again, with a throwaway onboard-gui key under a storage root inside the directory: it must pass and
#      reach the stack play's last task, so the onboard-gui's tasks after G9 are evaluated too.
#   6. bootstrap.yml --check, with KONTROLL_IN_CONTAINER=1 as the installer's `init` verb runs it (its compose
#      service sets it, docker/services/installer.yaml): the tasks that install the host's OS toolchain, which the
#      installer image bakes, are skipped. Those are the legacy host path's, and their check reads the host's own
#      packages (apt in check mode needs the host's python3-apt), not what a fresh node installs. It must pass and
#      reach its summary.
#   7. ping.yml and backup-configs.yml --check against tests/mock-inventory.yml (one fake host per shipped device
#      class, `ansible_connection: local`, RFC-5737 addresses): the fleet plays dispatch every class's `check` and
#      `backup` entrypoint and reach their summaries. No device is contacted and none answers: the step proves the
#      plays evaluate, not that a device does. ping.yml must pass, rescuing exactly the classes whose check reads a
#      device, each at that task and for that reason; backup-configs.yml must fail, at exactly the classes whose
#      capture does, each at that task (both lists below), and write no capture. Such a class is evaluated up to that
#      task, not past it. The API class needs a credential a fresh node has none of, so these two passes give its
#      host a placeholder token, and its check and capture must run past the GET a dry-run skips, to OK and to its
#      write. Each play then runs once more for that host alone, without the token, and must stop it at its
#      credential gate.
#
# What it never does: keep a file outside its temporary directory (HOME and TMPDIR are moved there before
# kontroll-init runs, Python writes no bytecode, and the tree it runs is the export, never this checkout; a task that
# becomes root works in root's ~/.ansible/tmp, as every `become` does, and an interrupt can leave a file there), start
# a container, or reach a device. Nothing here evaluates the docker_host class's opt-in backup, the armed trust mode
# (kontroll-init runs `--trust-mode separated`), or bootstrap's legacy host-OS toolchain (skipped, as the installer
# skips it). It is run by CI's validate job, not by tests/validate.sh: validate.sh also runs on a configured control
# node, where scaffolding a second instance is wrong; there (a /srv/kontroll.git, a /var/lib/kontroll) the deploy
# passes would read that node's canonical and storage, so the result says nothing about a fresh node. Run it by hand
# from a checkout on a machine that is not a control node: `bash tests/fresh-node-check.sh` (needs ansible-playbook
# with every module's collections, as the published runner image has them — `python3 scripts/gen-requirements.py
# <file> --all-modules`, then `ansible-galaxy collection install -r <file>` — sops, age-keygen and git, and for the
# deploy passes the `docker` Python library, the buildx plugin and a Docker daemon to read). KONTROLL_FRESH_KEEP=1
# keeps the directory and prints its path.
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
mkdir -p "$tmp/tree" "$tmp/home" "$tmp/tmp" "$tmp/logs" "$tmp/backups" "$tmp/storage/onboard-gui"

# The collections the runner installed live under the CALLER's home; the export's ansible.cfg names its own
# (empty) collections dir and HOME moves below, so name both before it does.
export ANSIBLE_COLLECTIONS_PATH="${ANSIBLE_COLLECTIONS_PATH:-$HOME/.ansible/collections:/usr/share/ansible/collections}"
export HOME="$tmp/home"
export SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt"
# A module's payload and Python's temporary files land in the directory too, and no bytecode is written beside the
# caller's collections.
export TMPDIR="$tmp/tmp" PYTHONDONTWRITEBYTECODE=1
# The export's ansible.cfg is the one read, and every log is plain text (the checks below parse them).
unset ANSIBLE_CONFIG ANSIBLE_FORCE_COLOR KONTROLL_IN_CONTAINER
export ANSIBLE_NOCOLOR=1
# The installer's launcher always names the operator (docker/services/installer.yaml requires it). Without one,
# local-canonical.yml takes the caller's uid, and a GitHub-hosted runner's is 1001, the network service's uid, which
# it refuses as an operator (Q-6). A fresh node's operator stands in; in check mode nothing is chowned to it.
export KONTROLL_OPERATOR_USER=fresh-operator KONTROLL_OPERATOR_UID=1000 KONTROLL_OPERATOR_GID=1000

git -C "$here" archive HEAD | tar -x -C "$tmp/tree"
tree="$tmp/tree"
# A fresh node's tree is a clone: a repository on `main`, which local-canonical.yml's landing reads as a worktree
# (its plaintext guard refuses a tree git cannot read). The export is made one, HEAD's tree its one commit.
git -C "$tree" init -q -b main
git -C "$tree" add -A
git -C "$tree" -c user.name=fresh-node-check -c user.email=fresh-node-check@example.invalid commit -qm "HEAD's tree"

# fail <name> <what>: the step did not do what it must — say how, print its log's last lines, name any collection it
# could not resolve (the mock inventory has a host for every class, so the check needs every module's collections,
# not one fleet's), and stop.
fail() {
  local missing
  echo "fresh-node-check: FAIL — $2; its log's last lines:" >&2
  tail -n 60 "$tmp/logs/$1.log" >&2
  missing=$(grep -Eo "couldn't resolve module/action '[^']+'" "$tmp/logs/$1.log" | sed -E "s/.*'([^']+)'$/\1/" \
            | sort -u | paste -sd, -) || true
  if [ -n "$missing" ]; then
    echo "fresh-node-check: $1 — no installed collection provides $missing: install every module's set" \
         "(python3 scripts/gen-requirements.py <file> --all-modules; ansible-galaxy collection install -r <file>)" >&2
  fi
  exit 1
}

# step <name> <expect: pass|fail> <cmd…>: run it from the export's ansible/ dir into its own log; a pass that was
# expected to fail, or the reverse, fails the check.
step() {
  local name=$1 expect=$2 rc=0
  shift 2
  (cd "$tree/ansible" && "$@") > "$tmp/logs/$name.log" 2>&1 || rc=$?
  if { [ "$expect" = pass ] && [ "$rc" -eq 0 ]; } || { [ "$expect" = fail ] && [ "$rc" -ne 0 ]; }; then
    echo "fresh-node-check: $name — ok (rc=$rc, expected $expect)"
    return 0
  fi
  fail "$name" "$name exited $rc, expected $expect"
}

# needs <name> <ERE> <what>: the step's log must hold the pattern (what the step is for, not just its exit).
needs() {
  if grep -Eq -- "$2" "$tmp/logs/$1.log"; then
    echo "fresh-node-check: $1 — says $3"
    return 0
  fi
  fail "$1" "$1's log does not say $3 (/$2/)"
}

# recap <name>: the step's log holds a PLAY RECAP and no host in it failed or was unreachable (a pass that skipped
# every play has none; a play that matched no host, or ran no task, is caught by the landmark `needs` after it).
recap() {
  if grep -q '^PLAY RECAP' "$tmp/logs/$1.log" && ! grep -Eq ' (failed|unreachable)=[1-9]' "$tmp/logs/$1.log"; then
    echo "fresh-node-check: $1 — its recap names no failed or unreachable host"
    return 0
  fi
  fail "$1" "$1's log has no PLAY RECAP, or a host in it failed or was unreachable"
}

# stops <name> [--rescued] <host=task>…: the step's log has a PLAY RECAP, the hosts it names as failed or
# unreachable (with --rescued: as rescued) are exactly the listed ones, and each stopped at the listed task (the task,
# or handler, of its last failure line that was not ignored; `role : name` as Ansible prints it). A mismatch prints
# what the log holds.
stops() {
  local name=$1 field=failed said="the hosts that failed"
  shift
  if [ "$1" = --rescued ]; then field=rescued said="the hosts it rescued"; shift; fi
  if python3 - "$tmp/logs/$name.log" "$field" "$@" <<'PY'
import re, sys
log, field, want = sys.argv[1], sys.argv[2], dict(a.split("=", 1) for a in sys.argv[3:])
text = open(log, encoding="utf-8", errors="replace").read()
ABSENT = object()
task, last, before, cur = None, {}, {}, None
for line in text.split("\n"):
    m = re.match(r"(?:TASK|RUNNING HANDLER) \[(.*)\] \*+$", line)
    if m:
        task, before, cur = m.group(1), {}, None
        continue
    m = re.match(r"(?:fatal|failed): \[([^\]\s]+)", line)
    if m:
        cur = m.group(1)
        before.setdefault(cur, last.get(cur, ABSENT))
        last[cur] = task
    elif line.strip() == "...ignoring" and cur is not None:
        if before[cur] is ABSENT:          # an ignored failure stopped nothing: the host's last stop is the one before
            del last[cur]
        else:
            last[cur] = before[cur]
        cur = None
recap = text.split("\nPLAY RECAP ", 1)[1] if "\nPLAY RECAP " in text else ""
rows = re.findall(r"^(\S+)\s+: ok=\d+ .*\bunreachable=(\d+) .*\bfailed=(\d+) .*\brescued=(\d+)", recap, re.M)
hit = sorted(h for h, u, f, r in rows if (int(r) if field == "rescued" else int(u) + int(f)))
got = {h: last.get(h) for h in hit}
if recap and got == want:
    sys.exit(0)
print("the recap's %s hosts and where each stopped: %s; listed: %s" % (field, got, want), file=sys.stderr)
sys.exit(1)
PY
  then
    echo "fresh-node-check: $name — $said, and where, are the listed ones"
    return 0
  fi
  fail "$name" "$name: $said are not the listed ones, each at its task. A class whose play must stop here (it reads \
its device, or needs a credential a fresh node has none of) is listed in this script as host=role : task"
}

step init pass python3 ../scripts/kontroll-init.py --fresh --mgmt-ip 192.0.2.10 --domain example.test \
  --trust-mode separated --no-break-glass
needs init '^==> kontroll-init done' "kontroll-init finished"
[ -f "$SOPS_AGE_KEY_FILE" ] && [ -f "$tree/instance/.sops.yaml" ] || {
  echo "fresh-node-check: FAIL — kontroll-init wrote no key at \$SOPS_AGE_KEY_FILE or no instance/.sops.yaml" >&2
  exit 1
}

all='["semaphore", "homepage", "prometheus", "grafana", "loki", "vector", "api", "onboard-gui"]'
last_task='^TASK \[Report the running stack containers\]'

step deploy-check pass env KONTROLL_IN_CONTAINER=1 ansible-playbook playbooks/deploy-stack.yml --check --diff
recap deploy-check
needs deploy-check "$last_task" "the stack play ran to its last task"

step deploy-check-all fail env KONTROLL_IN_CONTAINER=1 ansible-playbook playbooks/deploy-stack.yml --check --diff \
  -e "{\"stack_services\": $all}"
needs deploy-check-all "onboard-gui's age key at .* is MISSING" "the onboard-gui's age key is missing (G9)"

# The key G9 asks for, a throwaway, readable by the GUI's uid as G9 requires (other-read), under a storage root
# inside the directory: the default, /var/lib/kontroll, is the node's own.
age-keygen -o "$tmp/storage/onboard-gui/age.key" 2>/dev/null && chmod 0644 "$tmp/storage/onboard-gui/age.key" || {
  echo "fresh-node-check: FAIL — age-keygen wrote no throwaway onboard-gui key under $tmp/storage" >&2
  exit 1
}
step deploy-check-gui pass env KONTROLL_IN_CONTAINER=1 ansible-playbook playbooks/deploy-stack.yml --check --diff \
  -e "{\"stack_services\": $all, \"kontroll_storage_root\": \"$tmp/storage\"}"
recap deploy-check-gui
needs deploy-check-gui "$last_task" "the stack play ran to its last task"

step bootstrap-check pass env KONTROLL_IN_CONTAINER=1 ansible-playbook playbooks/bootstrap.yml --check
recap bootstrap-check
needs bootstrap-check '^TASK \[Summary\]' "bootstrap reached its summary"

# The API class's host, mock-fortigate, needs a token a fresh node does not have. The ping and backup passes give it
# a placeholder (nothing reads it: a dry-run skips the GET that would send it), so its check and its capture are
# evaluated past their credential gate; a pass of each without it, for that host alone, holds the gate.
api_token=mock_fortigate_api_token=placeholder

# ping.yml rescues a host its check cannot reach and reports it UNREACHABLE: a class whose check reads its device
# is rescued at that task. Held to the list, and each listed host to its reason, so a `check` that does not evaluate
# (a template error is rescued too, at the same task) cannot pass as a device that did not answer. mock-fortigate
# must say its GET was not made, and end OK.
step ping-check pass ansible-playbook -i ../tests/mock-inventory.yml playbooks/ping.yml --check -e "$api_token"
recap ping-check
needs ping-check '^PLAY \[Reachability summary\]' "the run reached its summary"
stops ping-check --rescued \
  "mock-switch=backend_netcommon_cli : Liveness via cli_command (network_cli)" \
  "mock-xe=backend_netcommon_cli : Liveness via cli_command (network_cli)"
needs ping-check '^fatal: \[mock-switch\]: .*Connection type local is not valid for this module' \
  "mock-switch was refused its network_cli connection"
needs ping-check '^fatal: \[mock-xe\]: .*Connection type local is not valid for this module' \
  "mock-xe was refused its network_cli connection"
needs ping-check '"msg": "mock-fortigate: the fortios check endpoint is not read in a dry-run' \
  "mock-fortigate's check went past its credential gate to the GET a dry-run skips"
needs ping-check '"msg": "mock-fortigate: OK"' "mock-fortigate ended OK"
# Without the token, mock-fortigate is rescued at its credential gate, for that reason.
step ping-check-gate pass ansible-playbook -i ../tests/mock-inventory.yml playbooks/ping.yml --check \
  --limit mock-fortigate
recap ping-check-gate
needs ping-check-gate '^PLAY \[Reachability summary\]' "the run reached its summary"
stops ping-check-gate --rescued \
  "mock-fortigate=backend_api : Refuse an unresolvable API credential (var NAMES only — never the value)"
needs ping-check-gate '"msg": "mock-fortigate: no API token is in scope' \
  "mock-fortigate stopped at its credential gate"
# The backup pass must FAIL, and only where the mock inventory stops it. backup-configs.yml rescues nothing (a
# capture that fails is a failed run), so under `ansible_connection: local` a class whose capture reads its device
# fails at that task. Every other host must pass and the run must reach its summary. A host failing anywhere else, or
# a listed one passing or failing at another task, fails the check: a class added to the mock inventory whose backup
# reaches a device is listed here, at that task. A listed host is held to its task, not its reason (its read is
# `no_log`, and ansible-core 2.18 prints no reason for it). mock-fortigate must reach its capture write.
step backup-check fail ansible-playbook -i ../tests/mock-inventory.yml playbooks/backup-configs.yml --check \
  -e "$api_token" -e "config_backup_dir=$tmp/backups"
needs backup-check '^PLAY \[Backup run summary\]' "the run reached its summary"
stops backup-check \
  "mock-edge=opnsense : Read /conf/config.xml" \
  "mock-switch=backend_netcommon_cli : Pull config via cli_command" \
  "mock-xe=backend_netcommon_cli : Pull config via cli_command"
needs backup-check '"msg": "mock-fortigate \(fortigate\): captured -> FortiGate_192-0-2-4_full-configuration\.conf"' \
  "mock-fortigate's capture went past its credential gate to its write"
# Without the token, mock-fortigate fails at its credential gate, for that reason. The run ends there: its one host
# failed, so Ansible runs no later play, and there is no summary to reach.
step backup-check-gate fail ansible-playbook -i ../tests/mock-inventory.yml playbooks/backup-configs.yml --check \
  --limit mock-fortigate -e "config_backup_dir=$tmp/backups"
stops backup-check-gate \
  "mock-fortigate=backend_api : Refuse an unresolvable API credential (var NAMES only — never the value)"
needs backup-check-gate '"msg": "mock-fortigate: no API token is in scope' \
  "the API class stopped at its credential gate"
# A --check run writes no capture: the writes preview an empty one, and the directory stays as it was.
[ -z "$(ls -A "$tmp/backups")" ] || fail backup-check "a --check run wrote a capture under $tmp/backups"

echo "fresh-node-check: PASS"
