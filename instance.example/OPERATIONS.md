# Operations — <this deployment>

**As of:** YYYY-MM-DD

Private: this file lives in the instance overlay and never ships (`tests/_leak_guard.py` does not scan `instance/`;
bundles, launch kits and images leave it out). It holds NO secret — it says where a credential lives (an entry
name, a key file's path), never its value. One copy is the ledger: the one in your instance repository. A control
node's own copy is whatever its canonical last received. The tool's own state is the repository's `STATUS.md`;
this file is the state of your deployment. `tests/check-status.py` warns while the date above is unfilled or more
than a month old.

## 1. Boxes and roles

| Role | VM id | Hypervisor node | Address | Branch @ commit deployed | Trust posture | Images | Last deploy | Last verified |
|---|---|---|---|---|---|---|---|---|
| production control node | 100 | node-a | 192.0.2.10 | main @ 0000000 | separated | published v0.0.0 | YYYY-MM-DD | YYYY-MM-DD |

## 2. Do not touch

The VM ids and hosts that must never be destroyed or reused, and for each the tool that owns it — so pruning goes
through that tool's own verbs.

## 3. Standing rules

Numbered. Each with *why* (the incident, dated) and *how to check it was followed*: provisioning rules (an address
probed free before it is assigned; the guest agent installed and running) and posture rules (when a box may be
re-armed, what a rebuild must pass, which backups restore an armed box).

## 4. Access paths

Per box and per hypervisor: how you and the control node reach it, the jump path, the key file's *path*, where the
passwords are (an entry name), and the out-of-band fallback per device — the four facts of a play's access-chain
header.

## 5. Key custody

Per age key: the secret domains it opens, where the private key is, each off-box copy and its medium, whether a
break-glass recipient exists, and the date of the last restore test.

## 6. Repositories and tines

The remotes, which branch each box follows, and the lag commands — the sync lag, any edition's lag, each box's
deployed commit against its branch — with the date each was last at zero.

## 7. Runbooks

Links only: rebuild, upgrade, disarm and re-arm, release, restore.

## 8. Owed lab actions

Every confirm-first action that is decided and not yet run: the exact command and the date it was decided.

## 9. Defects known here and not yet upstream

One row per defect that is only in a note: what, when found, the tree's state with the command that shows it, and
where it is tracked now. A closed row stays, with the closing commit.

## 10. Live-run log

Append-only, newest first: date, box, what ran, the result in one line. For the tool's maintainers: the newest
entry that ran `main` or the latest release is what `STATUS.md`'s "Last proven live" records.

## 11. Deploy gotchas

Short and dated: one sentence each, plus the fix or the doc that now carries it. A gotcha that has reached a
shipped document is deleted here.
