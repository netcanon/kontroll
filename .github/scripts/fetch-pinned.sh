#!/usr/bin/env bash
# fetch-pinned.sh <url> <sha256>  — a CI job's download of one pinned release file into the current directory,
#                                   retried on a transient error, then checked against its publisher's sha256
#
# A release mirror can fail one request and serve the next: one fetch in the validate job got an HTTP 504 after 2m40s
# and failed the job, and a re-run of that job alone, on the same commit, passed (2026-11-08). curl's --retry covers a
# timeout and an HTTP 408, 429, 500, 502, 503 or 504; --retry-all-errors adds the rest (a dropped connection, a short
# body). -f fails an HTTP error with its status, where curl would otherwise save the error page as the file and leave
# the digest check to fail it. The file is written with -o, never piped: curl starts the file again on each retry,
# where a pipe would hand its reader the bytes it already had. The sha256 check after it is the gate: a wrong or short
# file fails the step with sha256sum's status, whatever curl reported.
#
# FETCH_PINNED_TRIES (retries after the first try, default 3) and FETCH_PINNED_DELAY (seconds between tries, default
# 5) bound the wait; tests/unit/test_ci_workflows.py pins both and runs the helper against a local server.
set -euo pipefail
if [ "$#" -ne 2 ]; then
  echo "usage: fetch-pinned.sh <url> <sha256>" >&2
  exit 2
fi
file="$(basename "$1")"
curl -fsSL --retry "${FETCH_PINNED_TRIES:-3}" --retry-all-errors --retry-delay "${FETCH_PINNED_DELAY:-5}" \
  -o "$file" "$1"
echo "$2  $file" | sha256sum -c --quiet
