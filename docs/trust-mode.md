# `trust_mode` — how this instance promotes a staged proposal (the C10 second key)

kontroll's privileged surfaces (the onboard GUI, the typed API) never write the source of truth directly. They
**stage** a proposal — a `proposed/<run_id>` ref on the canonical git repo — and a **second key** fast-forwards
`main` only after the proposal is accepted. This propose-then-promote split (SECURITY.md **C10**) is what lets an
always-on, network-reachable service safely *suggest* a change to the fleet without being able to *apply* it.

`trust_mode` is a single per-instance setting (in `instance/instance.yml`) that chooses **who turns the second
key**. It is chosen **explicitly at fresh-init** and is **never silently defaulted** — see "never a default" below.

```yaml
# instance/instance.yml
trust_mode: separated   # or: solo
```

## The two postures

The table describes the two postures **as designed**. The `solo` column is what an edition that ships the
auto-promoter does; the Status box below says what this edition does.

| | `separated` (recommended / prod) | `solo` (homelab) |
|---|---|---|
| Who promotes a staged proposal | **A human** runs the `promote-proposal` Semaphore task | A separate **`kontroll-autopromoter`** runs the *same* promote automatically |
| Review before `main` advances | Yes — every proposal is looked at | No — the human-review keystroke is dropped |
| Feels like | propose → review → promote | one-click (onboard → live in seconds) |
| The GUI/API trust boundary | propose-only, AST read-only-pinned | **identical** — propose-only, AST read-only-pinned |

On an edition that ships it, the **only** thing `trust_mode` controls is whether the auto-promoter is deployed;
on this edition it controls nothing but the Settings row. It changes **no line** of the
GUI/API trust boundary: in **both** modes the network service's own code can only stage a proposal and is
read-only-by-construction (`tests/unit/_readonly_pins.py`); it never advances `main`. (The posture does not decide
how the canonical is mounted: `:ro` without `api_privileged`, `:rw` with it. Armed, what keeps the service's
pushes off `main` is `commit_and_push` — a staging service pushes `proposed/<run_id>` and nothing else — and the
canonical's `update` hook, which refuses a uid-1001 push to `main`. Neither binds a container RCE, which can write
the mounted repository directly: SECURITY.md C10, "Arming …".) `solo` adds a tiny, separate,
token-less automaton on the **trusted** side of the wall that runs the **already-shipped** FF-only + never-brick
(FIX-M9) promote — it drops the human keystroke, nothing else.

> **Status:** every edition ships the **flag** — the posture is chosen at init, shown in Settings and validated at
> deploy. **This edition has no auto-promoter:** `kontroll-init --fresh` refuses to run without `--trust-mode`, and
> choosing `solo` records the intent and behaves like `separated` (proposals stay staged for a manual
> `promote-proposal`). The auto-promoter that gives `solo` its effect belongs to the permissive (homelab) edition,
> which lives on a branch of the private instance repository and is never merged here.

## The honest `solo` residual (read before choosing `solo`)

This section describes `solo` **on an edition that runs the auto-promoter**. This edition has none, so the attack
path below does not exist here: a staged proposal waits for a human in both modes. It is kept because the same
`instance.yml` value means this on the permissive edition.

`solo` removes the human review gate, which creates an attack path that does **not** exist in `separated`:

> a GUI/API RCE stages a malicious proposal → the auto-promoter promotes it unattended → the next enact actuates it.

It is bounded — even in `solo` — by the inherited spine (FF-only history; never-brick/FIX-M9; a leaked token can
only stage; enact is still a separate hand-off) and, on an edition that runs the auto-promoter, by its guards
(a high-blast-tier refusal that keeps the **edge firewall + core switch human-gated even in solo**, a rate limit,
and a diff-size ceiling — a refusal never deletes the staged ref, so it degrades exactly to `separated` for that
proposal). None of that bounds a container RCE on an armed box: the canonical is then `:rw` to the service
containers, and an RCE can move `main` with no promoter at all, in either posture (SECURITY.md C10, "Arming …").
And the irreducible cost of `solo` is real: a small, in-tier, in-budget malicious change **is** auto-promoted in
`solo`. **`solo` is acceptable only for a homelab you fully control — single operator, mgmt-VLAN-only, never a
multi-tenant or internet-adjacent deployment.** Prod uses `separated`.

> **Corrected 2026-07-05 (the origin-push breach fix, SECURITY.md C10):** before the fix, a `push:true` write
> advanced `main` **directly** (the content clone's `origin` **is** the canonical, so `git push origin HEAD`
> fast-forwarded it), bypassing the auto-promoter's promote entirely — on `solo` that meant a change could reach
> `main` **without** the auto-promoter's blast-tier / rate / diff-size refusal, i.e. even a high-blast
> edge-firewall/core-switch change could auto-land ungated. The fix (`and not staging` in `gitio.commit_and_push`)
> routes **every** `main` advance on `solo` back through `promote_ref` (and thus the blast gate) — restoring the
> residual to exactly what this section describes; the fix makes `solo` *strictly more correct*. **Fork note:** the
> canonical `update` hook refuses any uid-1001 receive-pack push to `main`, so the homelab auto-promoter MUST keep
> advancing `main` via `promote_ref`/`update-ref` (it does — `update-ref` is not receive-pack, so the hook never
> fires on it); never switch the auto-promoter to a raw `git push …:main`.

## "Never a silent default" — defended in three places

A trust posture is the one setting kontroll refuses to guess (unlike `tls_mode`, which fail-*safes* to
`self_signed`). An unset `trust_mode` resolves to **neither** mode:

1. **Birth** — `kontroll-init --fresh` refuses to finish without `--trust-mode {separated|solo}` (the only place a
   fresh overlay is created).
2. **Template** — `instance.example/instance.yml` ships `trust_mode` **commented out**, so a copied-but-unedited
   overlay has no active value.
3. **Deploy** — `deploy-stack.yml` asserts `trust_mode in ['separated','solo']` before bringing the stack up; an
   unset/garbage value **hard-fails** the deploy with a pointed message.

## See also

- [SECURITY.md](../SECURITY.md) — C10 (privileged-mutation enablement / propose-then-promote). The auto-promote
  residual is C17, a control of the permissive edition; this edition has no such surface.
- [docs/privileged-mutation-enablement.md](privileged-mutation-enablement.md) — the C10 two-key spine.
- The design-of-record: [docs/reviews/2026-06-29-north-star-onboard/30-trust-mode-fork.md](reviews/2026-06-29-north-star-onboard/30-trust-mode-fork.md).
