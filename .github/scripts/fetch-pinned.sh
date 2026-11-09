#!/usr/bin/env bash
# fetch-pinned.sh <url> <sha256>  — a CI job's download of one pinned release file into the current directory,
#                                   retried on an error, then checked against its publisher's sha256
#
# A release mirror can fail one request and serve the next: one fetch in the validate job got an HTTP 504 after 2m40s
# and failed the job, and a re-run of that job alone, on the same commit, passed (2026-11-08). curl's --retry covers a
# timeout and an HTTP 408, 429, 500, 502, 503 or 504; --retry-all-errors adds every other error (a dropped
# connection, a short body, a 404 too). -f fails an HTTP error with its status, where curl would otherwise save the
# error page as the file and leave the digest check to fail it; -L follows the mirror's redirect to the file. The file
# is written with -o, never piped: curl starts the file again on each retry, where a pipe would hand its reader the
# bytes it already had. --no-progress-meter keeps the bar out of the log and each "Will retry" warning in it.
#
# The wait is bounded too, not just the tries: a mirror that takes the connection and then sends nothing, or stops
# mid-body with the socket open, held the job until its 360-minute default (I-34). --connect-timeout gives up on a
# connection after 30 s; --speed-limit/--speed-time give up on a transfer slower than 1 KiB/s for 60 s (curl's error
# 28, retried); --retry-max-time stops retrying 300 s in, and refuses a Retry-After that asks to wait past that.
#
# FETCH_PINNED_TRIES (retries after the first try, default 3) and FETCH_PINNED_DELAY (seconds between tries, default
# 5) bound the retries; FETCH_PINNED_STALL (the seconds a stall is given, default 60) is the tests' knob, to make a
# stall quick. tests/unit/test_ci_workflows.py pins all three, holds every workflow to the defaults, and runs the helper
# against a local server. The sha256 check is the last line and the gate: a wrong, short or missing file (curl can exit
# 0 with none, on a Retry-After past the cap) fails the step with sha256sum's status, whatever curl reported.
set -euo pipefail
if [ "$#" -ne 2 ]; then
  echo "usage: fetch-pinned.sh <url> <sha256>" >&2
  exit 2
fi
file="$(basename "$1")"
curl -fSL --no-progress-meter --connect-timeout 30 --speed-limit 1024 --speed-time "${FETCH_PINNED_STALL:-60}" \
  --retry "${FETCH_PINNED_TRIES:-3}" --retry-all-errors --retry-delay "${FETCH_PINNED_DELAY:-5}" --retry-max-time 300 \
  -o "$file" "$1"
echo "$2  $file" | sha256sum -c --quiet
