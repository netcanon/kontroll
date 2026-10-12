#!/usr/bin/env sh
# scripts/lib/plaintext-guard.sh — the content gate in front of a blanket `git add -A`.
#
# WHY: some commits take a whole worktree into the control node's canonical without a human reading the diff — the
# deploy's "instance state" commit (ansible/playbooks/local-canonical.yml) and the launch-kit launcher's capture
# (scripts/kontroll.sh). "Everything committed is ciphertext or public" was a comment there, not a check. A secret
# file written in an editor and never encrypted has the right NAME; one plaintext value added by hand to an
# encrypted file still has a `sops:` block; a decrypted copy, an editor backup, a key saved next to the file it
# opens — each is staged by `add -A`, pushed to the canonical, cloned from there by the runner and copied by every
# backup. This script is RUN (not sourced) just before such an add. It is read-only, it needs sh, git and the POSIX
# base tools (find, awk, tr, wc, cat) and nothing else (the launch-kit host has no python and no sops), and
# it never prints a byte of file content: a finding is a path, a rule and line numbers.
#
# Usage: plaintext-guard.sh [--tree] <worktree>
#   default  check what a blanket add would commit: untracked (not ignored), modified, and already-staged paths.
#            <worktree> must be the TOP of its git repository: `git add -A` stages the whole repository from
#            wherever it runs, and only at the top do the listings below name the same files.
#   --tree   check every file under the secrets directories, tracked or not (the tests/validate.sh seat; no git)
# Exit 0  nothing to refuse.
# Exit 3  at least one finding, one line each on stderr:
#             plaintext-guard: REFUSED <path> — <rule>[ (line N[, N…])]
# Exit 2  it could not look, with one sentence saying why: no such directory, not a git worktree git can read, not
#         the top level of its repository, a listing that did not arrive whole, a checker that did not finish.
# A caller treats EVERY non-zero status as a refusal: a guard that cannot look has checked nothing. The checker
# itself holds to that: it refuses unless the END of the list of files reached it — a listing or a `tr` that
# dies on the way would otherwise hand it a short list, and a short list reads as "nothing wrong".
#
# Rules:
#   secrets-dir-member    under instance/secrets/ and ansible/secrets/ only *.sops.yml, *.sops.yaml, README.md
#                         and .gitkeep may sit — a decrypted copy or an editor backup is refused by its name
#   not-sops-encrypted    a file named *.sops.yml / *.sops.yaml (not the `.sops.yaml` configuration file) must be
#                         a sops document in which every value has the shape of ciphertext — the predicate below
#   private-key-material  no candidate may hold an age secret key (the whole key, anywhere in a line — also after
#                         `SOPS_AGE_KEY=`), a line that BEGINS with a PEM / PGP private-key header, or such a
#                         header anywhere in a line when key material follows it DIRECTLY — after one literal `\n`
#                         on the same line (a quoted string), or at the start of the next line (a YAML block
#                         scalar). An armored block whose body begins after a blank line or after armor header
#                         lines — every PGP private-key block, a passphrase-protected legacy PEM — is therefore
#                         recognised only where its header begins a line.
#   unusual-file-name     in the default mode, a candidate whose name holds a line break cannot be listed safely,
#                         so it is refused (`--tree` does not count names: the commits do not go through it)
#
# The is-encrypted predicate. It assumes whole-file encryption, which is what the shipped `.sops.yaml` creation
# rules give. A top-level `sops:` mapping exists, and every OTHER line of the file — before it AND after it — is
# one of
#     a blank line, a document marker, or a comment that is only `#`
#     an encrypted comment            #ENC[AES256_GCM,data:…,iv:…,tag:…,type:comment]
#     a key that opens a block        <key>:
#     a key with a ciphertext value   <key>: ENC[AES256_GCM,data:…,iv:…,tag:…,type:…]      (also as a list item)
#     a key with an empty value       <key>: ""   ''   []   {}   null   ~                    (also as a list item)
# — the last three optionally followed by an encrypted comment (sops keeps a note on its value's line). These are
# the shapes the pinned sops writes; tests/integration/test_plaintext_guard_sops.py asks the installed sops.
#
# INSIDE the `sops:` block the structure is read, and the recipient entries are not:
#   - a line at the block's own indent must be one of the metadata keys sops writes (`attention`, which the Python
#     sops 1.x wrote, is not among them); a key that holds a list (`age`, `pgp`, `kms` …) has either no value or
#     `[]`, any other has a value;
#   - nothing may be indented less, or with a tab, or hold a lone carriage return or a Unicode line break;
#   - a line indented MORE — or a list dash, which old releases put at the indent of its key — must stand under
#     a list key that has no value: it is a recipient entry;
#   - and nothing follows `version: <value>`, which sops writes last.
# That closes every place a key can land when it is added by hand "at the end of the file": the block is last,
# its last line is `version: …`, and an editor gives the new line that line's indent — or one level more. sops
# itself ignores such a line and keeps decrypting, so nothing else would notice it.
#
# NOT read, by design:
#   - key NAMES (sops does not encrypt them). A key may not itself contain ": ", so that
#     `key: value more: ENC[…]` cannot pass as one long key — which also refuses the two key shapes sops can write
#     with one: a quoted key holding ": ", and a key long enough to be written as `? key` / `: value`.
#   - whether a token IS ciphertext: the guard reads shapes and holds no key. A value written by hand in the shape
#     of a token passes.
#   - the recipient entries of the `sops:` block (what stands under `age:`, `pgp:`, `kms:` …) and their armored
#     payloads; the VALUE of one of sops's own scalar metadata keys (a line named like one, placed before
#     `version:`, or text typed onto such a line — the last line included); a second `sops:` block. None is
#     where a line added by hand lands by accident.
#   - README.md and .gitkeep in a secrets directory, beyond the key rule: they are allowed by name.
# The path tests are case-sensitive and anchored at the worktree root (`instance/secrets/`, `ansible/secrets/`);
# the name test (`*.sops.yml`) and the key rule apply anywhere. An age key in lower case (age itself rejects it),
# a key file saved as UTF-16, a PEM header with nothing key-like after it, an inlined armored block (see the
# rule) and a key stored base64-encoded as a whole (a Kubernetes Secret manifest) are not recognised as keys.
# A creation rule with `unencrypted_suffix` or `encrypted_regex` leaves values in the clear on purpose; a value it
# leaves in the clear is refused here like any other plaintext. Anything this predicate cannot recognise is
# refused: it fails closed.
set -eu
LC_ALL=C
export LC_ALL                 # names and file contents are bytes to every tool below, whatever the caller's locale

say() {                       # printf, not echo: some shells interpret a backslash in echo, and a path may hold one
  printf '%s\n' "plaintext-guard: $1" >&2
}

cannot_run() {
  say "$1 — refusing"
  exit 2
}

mode=candidates
if [ "${1:-}" = "--tree" ]; then
  mode=tree
  shift
fi
root="${1:-}"
if [ -z "$root" ] || [ ! -d "$root" ]; then
  say "usage: plaintext-guard.sh [--tree] <worktree>"
  exit 2
fi

# The mark that ends a list of files. Each lister prints it LAST, and only after everything before it succeeded;
# the checker refuses unless it arrives. No listing can produce it by itself: git and find print relative paths.
end_of_list=/plaintext-guard/end-of-list

# What a blanket add would put into the commit: untracked-and-not-ignored, modified, and whatever is staged already
# (a file somebody `git add`ed earlier is neither untracked nor modified, and the commit takes it all the same).
staged_paths() {
  git -C "$root" ls-files -z --others --modified --exclude-standard &&
    git -C "$root" diff --cached --name-only -z &&
    printf '%s\000' "$end_of_list"
}

odd=0
if [ "$mode" = candidates ]; then
  # git's own message goes through to the caller: "dubious ownership" is not "not a repository".
  inside=$(git -C "$root" rev-parse --is-inside-work-tree) || inside=
  [ "$inside" = true ] || cannot_run "git cannot read $root as a worktree (its own message, if any, is above)"
  # `ls-files` lists below the directory it runs in; `diff --cached` and `add -A` work on the whole repository.
  # The three name the same files only at the top level. (A git that FAILS to answer gives an empty string too,
  # so the answer and the failure are told apart.)
  cdup=$(git -C "$root" rev-parse --show-cdup) ||
    cannot_run "git could not say where the top of this repository is (git rev-parse --show-cdup)"
  [ -z "$cdup" ] ||
    cannot_run "$root is inside a git repository but is not its top level: a blanket add here takes files this check cannot list"
  # Ask each listing once on its own, for a message that names it; the end mark below is what catches a failure
  # in the runs whose output is actually used.
  git -C "$root" ls-files -z --others --modified --exclude-standard >/dev/null ||
    cannot_run "git could not list the untracked and modified files (git ls-files)"
  git -C "$root" diff --cached --name-only -z >/dev/null ||
    cannot_run "git could not list what is already staged (git diff --cached)"
  # Paths come NUL-separated; the checker reads lines. A name with a line break in it would arrive as two names
  # that are not the file, so count both ways and refuse when they differ.
  entries=$(( $(staged_paths | tr -cd '\000' | wc -c) ))
  lines=$(( $(staged_paths | tr '\000' '\n' | wc -l) ))
  if [ "$entries" -ne "$lines" ]; then
    say "REFUSED (a path with a line break in its name) — unusual-file-name"
    odd=1
  fi
fi

candidates() {
  if [ "$mode" = tree ]; then
    for dir in instance/secrets ansible/secrets; do
      if [ -d "$root/$dir" ]; then
        # -H: the secrets directory itself may be a symlink. A find that fails part-way has listed part.
        (cd "$root" && find -H "$dir" -type f -print) || return 1
      fi
    done
    printf '%s\n' "$end_of_list"
  else
    staged_paths | tr '\000' '\n'
  fi
}

# Only regular files are read. A deleted path is listed as "modified" and has nothing to read; git stores a
# symlink as its target's NAME, not its content. The end mark is passed on.
existing() {
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    if [ "$path" = "$end_of_list" ]; then
      printf '%s\n' "$path"
      continue
    fi
    [ -f "$root/$path" ] || continue
    [ -L "$root/$path" ] && continue
    printf '%s\n' "$path"
  done
}

status=0
candidates | existing | PG_ROOT="$root" PG_END="$end_of_list" awk '
function report(name, rule, extra) {
  print "plaintext-guard: REFUSED " name " \342\200\224 " rule extra | ERR
  found++
}

# A lone CR, NEL, LS or PS is a line break to a YAML reader and not to awk: one "line" here would be two there.
function breaks(line) {
  return (line ~ /\r/ || index(line, NEL) || index(line, LS) || index(line, PS))
}

# A mapping key as sops writes it: not empty, and holding no ": " — with one inside, the text before it is a key
# and the text after it a plaintext VALUE that only looks like part of a key.
function key_ok(key) {
  return (key !~ /^[ \t]*$/ && key !~ /:[ \t]/)
}

# 1 when the line is one the predicate allows in the document body (outside the sops block).
function allowed(line,    s, key, dashed) {
  if (line ~ /^[ \t]*$/) return 1
  if (breaks(line)) return 0
  if (line ~ /^---[ \t]*$/ || line ~ /^\.\.\.[ \t]*$/) return 1
  s = line
  sub(/^[ \t]+/, "", s)
  if (substr(s, 1, 1) == "#") return (s ~ /^#[ \t]*$/ || s ~ ("^#" ENC "[ \t]*$"))
  sub("[ \t]+#" ENC "[ \t]*$", "", s)                           # a note kept on the line, as an encrypted comment
  dashed = (s ~ /^-[ \t]+/)
  while (s ~ /^-[ \t]+/) sub(/^-[ \t]+/, "", s)                  # list dashes, nested ones too
  if (s == "-") return 1                                        # a bare dash opening a nested item
  if (s ~ ("^" ENC "[ \t]*$")) return 1                         # a list item that is one token
  if (dashed && s ~ ("^" EMPTY "[ \t]*$")) return 1             # a list item that is an empty value
  if (match(s, ":[ \t]+" ENC "[ \t]*$")) return key_ok(substr(s, 1, RSTART - 1))
  if (match(s, ":[ \t]+" EMPTY "[ \t]*$")) return key_ok(substr(s, 1, RSTART - 1))
  if (s ~ /:[ \t]*$/) {                                         # <key>: opening a block
    key = s
    sub(/:[ \t]*$/, "", key)
    return key_ok(key)
  }
  return 0
}

# 1 when a line inside the sops block is NOT what sops writes there (see the header). `sopsind` is the indent of
# the block, `under` says the last key at that indent was a list key with no value (its entries follow), `done`
# that `version: <value>` has been seen (nothing follows it).
function foreign(line,    ind, body) {
  match(line, /^[ \t]+/)
  ind = RLENGTH
  if (sopsind == 0) sopsind = ind
  body = substr(line, ind + 1)
  if (breaks(line) || line ~ /^ *\t/ || ind < sopsind || done) return 1
  if (ind > sopsind || body ~ /^-/) return !under               # a recipient entry: only under a list key
  under = 0
  if (body ~ LISTKEY) {                                         # no value (entries follow) or an empty list
    if (body ~ /^[a-z_]+:[ \t]*$/) { under = 1; return 0 }
    return (body !~ /^[a-z_]+:[ \t]+\[\][ \t]*$/)
  }
  if (body ~ SCALARKEY) {                                       # always has a value
    if (body ~ /^[a-z_]+:[ \t]*$/) return 1
    if (body ~ /^version:/) done = 1
    return 0
  }
  return 1
}

function check_sops(name, file,    line, n, rc, insops, seen, bad, nbad, wrong) {
  n = 0; insops = 0; seen = 0; bad = ""; nbad = 0
  while ((rc = (getline line < file)) > 0) {
    n++
    sub(/\r$/, "", line)                                        # a file saved with CRLF line ends
    if (n == 1) sub(BOM, "", line)                               # ...or with a byte-order mark
    if (insops && line ~ /^[ \t]*$/) continue                    # a blank inside the block (a PGP armor has one)
    if (insops && line ~ /^[ \t]/) {
      wrong = foreign(line)
    } else {
      insops = 0                                                # a line in column 0 ends the block: check it
      if (line ~ /^sops:[ \t]*$/) { seen = 1; insops = 1; sopsind = 0; under = 0; done = 0; continue }
      wrong = !allowed(line)
    }
    if (wrong) {
      nbad++
      if (nbad <= 5) bad = bad (bad == "" ? "" : ", ") n
    }
  }
  close(file)
  if (rc < 0) { report(name, "not-sops-encrypted", " (unreadable)"); return }
  if (!seen) { report(name, "not-sops-encrypted", " (no sops block)"); return }
  if (nbad > 0) report(name, "not-sops-encrypted", " (line " bad (nbad > 5 ? " and more" : "") ")")
}

# A key is: an age identity in full, anywhere in a line; a PEM / PGP private-key header that begins its line; or
# such a header anywhere in a line when key material follows it DIRECTLY — after one literal \n on the same line
# (a quoted string), or at the start of the next line (a block scalar). A header that is only quoted in a document
# has neither, and is not a key. (No apostrophe may stand in this program, comments included: it would end the
# single quote the shell passes it in.)
function check_keys(name, file,    line, n, rc, hit, pending, t) {
  n = 0; pending = 0
  while ((rc = (getline line < file)) > 0) {
    n++
    if (n == 1) sub(BOM, "", line)
    hit = (index(line, "AGE-SECRET-KEY-") && line ~ AGEKEY)
    if (!hit && index(line, "PRIVATE KEY")) hit = (line ~ ("^" HDR) || line ~ (HDR "\\\\n" RUN20))
    if (!hit && pending) {
      t = line
      sub("^[ \t\"\047]+", "", t)
      if (t ~ ("^" RUN40)) { hit = 1; n-- }                      # report the line of the header
    }
    if (hit) {
      report(name, "private-key-material", " (line " n ")")
      break
    }
    pending = (index(line, "PRIVATE KEY") && line ~ HDR)
  }
  close(file)
  if (rc < 0) report(name, "private-key-material", " (unreadable)")
}

BEGIN {
  ROOT = ENVIRON["PG_ROOT"]
  END_MARK = ENVIRON["PG_END"]
  ERR = "cat 1>&2"
  B64 = "[A-Za-z0-9+/=]"
  ENC = "ENC\\[AES256_GCM,data:" B64 "*,iv:" B64 "+,tag:" B64 "+,type:[a-z]+\\]"
  BOM = "^\357\273\277"
  NEL = "\302\205"; LS = "\342\200\250"; PS = "\342\200\251"
  EMPTY = "(\"\"|\047\047|\\[\\]|[{][}]|null|~)"
  # The first-level keys of the metadata block, as the pinned sops (3.13) names them: the ones that hold a list
  # of recipient entries, and the scalars. A release that adds one makes this a refusal of a healthy file — the
  # direction a control fails in — and the conformance test, which reads the installed binary, says so first.
  LISTS = "key_groups|kms|gcp_kms|hckms|azure_kv|hc_vault|age|pgp"
  SCALARS = "shamir_threshold|lastmodified|mac|unencrypted_suffix|encrypted_suffix|unencrypted_regex"
  SCALARS = SCALARS "|encrypted_regex|unencrypted_comment_regex|encrypted_comment_regex|mac_only_encrypted|version"
  LISTKEY = "^(" LISTS "):"
  SCALARKEY = "^(" SCALARS "):"
  # An age identity in full: the prefix, then at least the 58 characters every one has (the hybrid post-quantum
  # identities of age 1.3 carry `PQ-` in the prefix). A shorter word in a test or a document is not a key.
  AGEKEY = "AGE-SECRET-KEY-(PQ-)?1"
  for (i = 0; i < 58; i++) AGEKEY = AGEKEY "[QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L]"
  HDR = "-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----"
  RUN20 = ""; for (i = 0; i < 20; i++) RUN20 = RUN20 "[A-Za-z0-9+/]"
  RUN40 = RUN20 RUN20
  found = 0; whole = 0; late = 0
}

# The end mark is the LAST line of a list that arrived whole. (No sort stands between the listers and this
# program: a path is checked once however often it is listed, and the mark stays last.)
$0 == END_MARK { whole = 1; next }
whole { whole = 0; late = 1 }
listed[$0]++ { next }

{
  cand = $0
  file = ROOT "/" cand
  base = cand
  sub(/^.*\//, "", base)
  named_sops = (base ~ /\.sops\.ya?ml$/)
  if ((cand ~ /^instance\/secrets\// || cand ~ /^ansible\/secrets\//) &&
      !(named_sops || base == "README.md" || base == ".gitkeep")) {
    report(cand, "secrets-dir-member", "")
  }
  if (named_sops && base != ".sops.yaml") check_sops(cand, file)
  check_keys(cand, file)
}

END {
  close(ERR)
  if (!whole || late) exit 4
  exit (found ? 3 : 0)
}
' || status=$?

if [ "$status" -eq 4 ]; then
  cannot_run "the list of files to check did not arrive whole (a listing or a pipe stage failed)"
fi
if [ "$status" -ne 0 ] && [ "$status" -ne 3 ]; then
  cannot_run "the check itself did not finish (awk exit $status)"
fi
if [ "$status" -eq 3 ] || [ "$odd" -ne 0 ]; then
  exit 3
fi
exit 0
