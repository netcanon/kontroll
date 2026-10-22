#!/usr/bin/env bash
# apt-install.sh <package>...        — a CI job's `apt-get update` + `install`, retried while another process holds
#                                      apt's lock
# apt-install.sh --run <command>...  — any command that runs apt itself (`playwright install --with-deps`), retried
#                                      the same way
#
# The self-hosted runners on one host share /var/lib/apt, /var/cache/apt and /var/lib/dpkg, so a concurrent job's apt
# (or the host's own apt-daily and unattended-upgrades) can hold their locks. `update` fails at once on the lists lock
# and `install` on the archives lock, whatever DPkg::Lock::Timeout says; `install` waits up to that timeout for dpkg's
# two locks, then fails (measured, apt 2.6.1, 2.8.3 and 3.0.3). On any of those four lock errors the step runs again.
# A failure that names no lock (a package that does not exist, a mirror that is down, a lock file apt may not open)
# fails at once with its own exit status, as the bare command did. Only apt's `E:` line counts: its wait for dpkg's
# lock prints "Could not get lock" too, and its "Unable to lock directory" follows a lock it could not open as well
# as one another process holds.
#
# LC_ALL=C: apt's messages are translated, and the pattern is the C locale's. The export reaches a `--run` command
# (and its own `sudo apt-get`, as sudo keeps LC_* by default); the pair sets it past sudo itself, whatever the
# host's sudoers keeps. APT_INSTALL_TRIES (default 20) and APT_INSTALL_DELAY (seconds, default 15) bound the wait,
# with dpkg's 60 s on each try; tests/unit/test_ci_workflows.py pins all three.
set -uo pipefail
export LC_ALL=C

tries="${APT_INSTALL_TRIES:-20}"
delay="${APT_INSTALL_DELAY:-15}"

apt_pair() {
  sudo env LC_ALL=C apt-get update &&
    sudo env LC_ALL=C apt-get -o DPkg::Lock::Timeout=60 install -y --no-install-recommends "$@"
}

if [ "${1:-}" = "--run" ]; then
  shift
  step=("$@")
else
  step=(apt_pair "$@")
fi
if [ "$#" -eq 0 ]; then
  echo "usage: apt-install.sh <package>... | apt-install.sh --run <command>..." >&2
  exit 2
fi

out="$(mktemp)" || exit 1
trap 'rm -f "$out"' EXIT
for ((try = 1; try <= tries; try++)); do
  "${step[@]}" 2>&1 | tee "$out"
  rc=${PIPESTATUS[0]}
  [ "$rc" -eq 0 ] && exit 0
  grep -qE '^E: (Could not get lock |.*, is another process using it\?$)' "$out" || exit "$rc"
  [ "$try" -lt "$tries" ] || break
  echo "apt-install: another process holds apt's lock (try $try of $tries); again in ${delay}s" >&2
  sleep "$delay"
done
echo "apt-install: apt's lock was still held after $tries tries" >&2
exit 1
