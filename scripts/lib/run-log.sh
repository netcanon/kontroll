# Shared run-log helper for operator-run kontroll scripts (Layer 0 logging,
# docs/logging-architecture.md §3). Source it, then call `run_log_init <name>` once near the
# top of a script: from that point the script's stdout+stderr are tee'd to a timestamped log
# in an OPERATOR-writable dir (these scripts run as the operator, NOT the uid-1001 runner), and
# logs older than KONTROLL_RUNLOG_RETENTION_DAYS (default 30) are pruned. Gives a persistent
# "what ran, when, succeeded?" trail.
#
#   KONTROLL_RUN_LOG_DIR           override the dir (default ~/.local/state/kontroll/runs, the one Vector ships when
#                                  the script runs as the operator who deployed the stack)
#   KONTROLL_RUNLOG_RETENTION_DAYS prune age in days (default 30; empty/non-numeric -> 30, fail-closed)
#   RUN_LOG_FILE                   set by run_log_init -> this run's log path
#
# Secret-safety is the CALLER's responsibility — never echo a credential (no redaction here).
# Tee uses a process substitution; the final buffered lines can be lost if the shell exits
# abruptly — acceptable for an ops run-log (the durable record of intent + outcome, not a ledger).
#
# Vector ships the run-logs from one directory: docker/services/vector.yaml binds
# <operator home>/.local/state/kontroll/runs, the home being KONTROLL_OPERATOR_HOME as the deploy wrote it, and the
# launchers and the deploy make it. The default is that directory when the script runs as that operator ($HOME):
# run as root, or under sudo, $HOME is root's, the log is kept under it, Vector does not read it, and nothing here
# can tell (I-46). XDG_STATE_HOME is not read: a run-log under it was kept and never shipped, with no error anywhere
# (I-29). A run under KONTROLL_RUN_LOG_DIR says, in its first lines, that its log is kept there (the directory
# spelled absolute) and not shipped; the shipped directory spelled another way (`//`, `..`, a symlink) is the same
# directory and says nothing. With HOME unset and no KONTROLL_RUN_LOG_DIR there is no run-log; the script goes on.

run_log_init() {
  local name="${1:-run}"
  local shipped="${HOME:+$HOME/.local/state/kontroll/runs}"   # ${HOME:+}: the callers run `set -u`
  local dir="${KONTROLL_RUN_LOG_DIR:-$shipped}"
  [ -n "$dir" ] || return 0                         # fail-soft: no HOME, no override -> no run-log, script proceeds
  mkdir -p "$dir" 2>/dev/null || {                 # fail-soft: no dir -> no run-log, script proceeds
    echo "run-log: cannot make $dir — continuing without a run-log" >&2; return 0; }
  local ts
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  RUN_LOG_FILE="$dir/${name}-${ts}.log"
  : > "$RUN_LOG_FILE" 2>/dev/null || {           # fail-soft: unwritable (full/RO disk) -> no run-log
    echo "run-log: cannot write $RUN_LOG_FILE — continuing without a run-log" >&2; return 0; }
  exec > >(tee -a "$RUN_LOG_FILE") 2>&1            # tee all later output -> console + the log
  echo "# kontroll $name run $ts (pid $$)"
  if ! [ "$dir" -ef "$shipped" ]; then               # -ef: the same directory however it is spelled
    echo "# run-log: kept in $(cd "$dir" && pwd) (KONTROLL_RUN_LOG_DIR), which Vector does not read; it ships" \
      "${shipped:-<operator home>/.local/state/kontroll/runs}"
  fi
  # Retention: KONTROLL_RUNLOG_RETENTION_DAYS (default 30). FAIL-CLOSED — empty/non-numeric clamps to 30 so a
  # garbage value can never become a malformed `-mtime` (which would error, or worse match nothing).
  local days="${KONTROLL_RUNLOG_RETENTION_DAYS:-30}"
  case "$days" in '' | *[!0-9]*) days=30 ;; esac
  find "$dir" -name '*.log' -type f -mtime "+$days" -delete 2>/dev/null || true
}
