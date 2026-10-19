# GUI-actuatable onboarding + observability flow

> **DESIGN HISTORY — not a description of the tree.** This is the design record of the GUI-actuatable onboarding and observability flow, written in June 2026. The seam it designs shipped; the as-built reference is [secondary-capability-dialog.md](secondary-capability-dialog.md). Where it disagrees with the code or with `SECURITY.md`, they are right.

> **Part of the [Option-A observability + onboarding flow](../observability-onboarding-flow.md) design. This is INSTANCE #1 (telemetry) of the generalized secondary-capability seam, which SHIPPED in Capability-track Phase 7 — see the [seam reference-of-record](secondary-capability-dialog.md) for the as-built state. This section remains the telemetry instance's UX design rationale; where it predicts a future tense, the seam is now built.**
>
> ⚠️ **Binding contracts (the catalog loader name, the flat `telemetry/<method>.yml` schema, the single clean-cutover dispatcher, the `network` job-vs-inventory-group distinction, the `.env` secret-render mechanism) are pinned in the master design's §4.0 SHARED CONTRACTS.** Where this section names a symbol, schema, or sequencing differently, **§4.0 wins** and this section is reconciled to it before implementation. See the master design's §8 “Resolved conflicts” for the per-section reconciliation list.
>
> 🧩 **This is INSTANCE #1 (telemetry/monitoring) of the generalized [secondary-capability dialog seam](secondary-capability-dialog.md).** The capability-neutral contract — the `capabilities/<cap>.yml` descriptor, the registry, `promote.py`, INVARIANT D\*, the generic `openCapabilityDialog`/`/api/capability` shell — lives in the seam design-of-record + master §4.0. **As built (Phase 7):** the dialog builder is the generic `openCapabilityDialog("telemetry", key, …)`; the testids are the capability-neutral `cap-*` set (`cap-dialog`/`cap-method`/`cap-propose`/`cap-promote`/`cap-plan`/…, recorded in [tests/testid_reference.md](../../tests/testid_reference.md)) — NOT a telemetry-specific `observe-*` set; the route is `POST /api/capability/telemetry`. Where this section says `observe-*` / `/observe`, read the generic `cap-*` / `/capability/telemetry`.

This section designs the **operator-facing flow** by which per-device/service observability becomes GUI-actuatable: the onboarding GUI extension (observability opt-in, telemetry-method picker filtered to applicable methods, dashboard picker), the API routes that back it, a **minimal propose-then-promote** primitive (because none exists in code today), the audit/rate-limit/fail-closed posture, and **exactly** what each confirm actuates. It is grounded in the real code read for this dimension: `gui/app.py`, `gui/templates/index.html`, `api/routes/onboard.py`, `api/auth.py`, `api/audit.py`, `api/ratelimit.py`, `api/main.py`, `api/deps.py`, `api/settings.py`, `scripts/kontroll/service/onboard.py`, `scripts/galaxy.py` (`cmd_onboard`), `scripts/gen-observability.py`, `scripts/fetch-dashboards.py`, `ansible/playbooks/install-node-exporter.yml`, and the test/testid scaffolding.

## 0. The honest boundary (read this first — it constrains everything below)

Three facts from the real code shape the entire design, and the design **must not pretend otherwise**:

1. **The privileged API is not deployed and the GUI does not call it.** `api/main.py` docstring: *"Still not deployed — mgmt-VLAN-only"*; `require_token` returns **503** when `KONTROLL_API_TOKEN` is unset (`api/auth.py:41-43`). The GUI's `/api/onboard` **shells out to `galaxy.py onboard`** via `run_galaxy()` (`gui/app.py:111,188`) — it does *not* call the in-process API onboard route. So there are **two actuation implementations** (GUI→CLI, API→service layer) and the GUI silo's audit is **fail-open** 4-field TSV (`gui/app.py:85-93`, swallows `OSError`), whereas the API silo is **fail-closed** 6-field with a `run_id` (`api/audit.py`).

2. **The API deliberately does not run Ansible.** `api/routes/onboard.py:5-6` and its docstring: *"bootstrap (collection install + live-verify) is DELEGATED to Semaphore — the API does not run Ansible."* Installing `node_exporter`, provisioning an exporter container (`docker compose up` + a SOPS-decrypted `.env` render), and reloading Prometheus/Grafana are **exactly** the Ansible/Docker actuation the API was built to *not* do.

3. **`apply_onboard_plan` touches no observability.** `scripts/kontroll/service/onboard.py:89-107` writes the module, the drop-in host, `enable_in_fleet`, and SOPS creds — and **nothing** about `metrics:`/`dashboards:`/targets/exporters. `proxmox`/`docker_host` got their `metrics:` blocks **by hand**.

**The boundary this design draws, stated once:**

> **PROPOSE (data-write, in scope for GUI/API):** write the module's `metrics:`/`dashboards:` block into the working tree, regenerate the Prometheus target lockfile (`gen-observability.py`, pure stdlib, `become:false`, no lab), surface the diff, and commit it (audited, run_id-correlated).
>
> **ENACT (infra-touching, OUT of the network surface):** install the host agent, provision/stand-up the exporter container + render its secret into `.env`, fetch the dashboard JSON (online), reload Prometheus/Grafana. This stays **operator-CLI / Semaphore / `deploy-stack`** — the established delegated path. The GUI's contribution is to *emit the deterministic next-step commands and (optionally) trigger the same Semaphore task the bootstrap flow already uses*, never to run plays in-process.

This keeps the keystone GUI brick (the data-write proposal) inside the **current** trust boundary and lets it ship **without** the deferred privileged-mutation enablement (writable clone + age-key mount + token) that `SECURITY.md` C9 and the 1a/1b roadmap defer. The self-review's two blockers (propose-then-promote is vapor; the API must not run Ansible) are honored by construction.

This design depends on **Layer 1** (the telemetry-method registry: `telemetry/<method>.yml`, `paths.TELEMETRY_DIR`, `catalog.load_telemetry()`, the `gen-observability.py` dispatcher) being **already implemented** by the modularity dimension. Where this flow references a method descriptor field, it is the field that dimension defines. This section treats the registry as an input and designs **on top of it**.

---

## 0.1 The decoupling invariant

> **INVARIANT D (Decoupling).** Observability setup can never gate, block, delay, or complicate making a device/service Ansible-usable. Concretely, all hold and are test-pinned:
> 1. No observability input is required to onboard (onboard fields stay `collection,key,group,host` + creds).
> 2. The onboard data-write touches zero observability artifacts (`apply_onboard_plan`, `onboard.py:89-107`, grep=0).
> 3. The onboard result is independent of observability state.
> 4. A device is fully Ansible-usable after onboard alone (module + host + fleet-enable + creds is the complete set the `device_role` seam needs).
> 5. Observe is strictly-after and optional (`/observe` 404s `no_module` on an un-onboarded class).
> 6. The nudge is render-only and non-binding.

This is the second load-bearing separation in this design, co-located with the §0 PROPOSE-vs-ENACT boundary: **onboarding (make a device Ansible-usable) and observability (the `metrics:`/`dashboards:` data-write) are different routes, different service functions, different data-writes.** The data layer already enforces it — `apply_onboard_plan` writes module + drop-in host + `enable_in_fleet` + SOPS creds and **nothing** about `metrics:`/`dashboards:`/targets. The standalone dialog (§5) keeps the GUI honest to that; the tests below keep it from silently re-coupling.

### Adversarial coupling enumeration (8 structurally impossible, 8 guardrailed)

| # | Path | Verdict | Why / guardrail |
|---|---|---|---|
| C1 | Shared route fails onboard on observe error | IMPOSSIBLE | `/api/onboard` (`gui/app.py:158`) vs drop-in `/api/observe`; `api/routes/onboard.py` vs `observe.py` |
| C2 | `apply_onboard_plan` calls the observe writer | IMPOSSIBLE | it calls only `_write_new`/`enable_in_fleet`/`sops_set` (`onboard.py:89-107`) |
| C3 | A metrics block written inside `build_onboard_plan` (the spine narrative) | GUARDRAIL | reword spine §4 row 1 / §5.1 / §5.2 so `attach_observability` is invoked by the *observe* path, never inside the onboard builder; pin with the import tripwire |
| C4 | Required observability field on onboard | GUARDRAIL | onboard 400-required set asserted == `{collection,key,group,host}`; CLI `onboard` argparse has no `--method/--observe` flag |
| C5 | Failed suggest aborts onboard | GUARDRAIL | nudge computed after success, wrapped `try/except → None`; never sets the response error/ok |
| C6 | Down exporter blocks onboard | IMPOSSIBLE | enact is returned as strings, never run (§4 table) |
| C7 | Missing dashboard id blocks onboard | IMPOSSIBLE | dashboards are an observe input only; honest-empty even there |
| C8 | New SOPS domain must exist before onboard | IMPOSSIBLE | onboard writes only its own creds domain; exporter `secret_domain` is referenced at enact time only |
| C9 | A modal nudge blocks the UI | GUARDRAIL | nudge is non-modal, dismissible, inline; e2e proves onboarding is "done" with it un-clicked |
| C10 | Suggest error surfaces as onboard error | GUARDRAIL | same as C5; nudge never populates error/ok |
| C11 | Picker 404 reads as "onboard failed" | GUARDRAIL (framing) | picker called only in the dialog, after onboard; honest copy |
| C12 | Shared promote checkbox (`field-apply`) triggers observe | GUARDRAIL | dialog's own `observe-apply`; e2e asserts ticking onboard `field-apply` fires zero `/api/observe` |
| C13 | A validate step requires a metrics block for an onboarded class | IMPOSSIBLE/GUARDRAIL | the flipped honesty test already encodes "no metrics block → no target, valid" |
| C14 | Observe reachable only from a live search card | GUARDRAIL | EP-3 keys on an existing `key` independent of search; e2e opens the dialog with no prior search |
| C15 | Fleet-enable depends on observe | IMPOSSIBLE | `enable_in_fleet` runs inside `apply_onboard_plan` (`onboard.py:98`) |
| C16 | Audit/run_id coupling aborts a shared transaction | IMPOSSIBLE | separate routes, separate audit calls |

### Pinning tests — AS BUILT in [`tests/unit/test_invariant_d.py`](../../tests/unit/test_invariant_d.py)

The INVARIANT D pins shipped capability-NEUTRAL (D → D\*), superseding the telemetry-named sketch this
section first proposed:
- `test_onboard_apply_writes_only_known_keys` — the PRIMARY positive-allow-list pin: the written module.yml's
  top-level keys are a subset of the onboard-allowed set, which structurally excludes `metrics:`/`backup:`/any
  capability block **without iterating the registry** (so it survives an empty/broken registry — it can't
  vanish). Replaces the telemetry-named `test_onboard_apply_writes_no_observability_keys`.
- `test_onboard_apply_writes_no_capability_block[C]` — the SECONDARY registry-parametrized pin over
  `registered_capabilities()`, with `test_secondary_pins_are_not_vacuous` (min-count guard).
- `test_onboard_survives_a_suggester_failure` — onboarding completes even when the telemetry suggester raises
  (`build_onboard_plan` makes the nudge best-effort); `test_capability_suggester_does_not_mutate_the_repo[C]`
  pins suggester read-only-ness.

**`tests/integration/test_decoupling_api.py`:**
- `test_onboard_route_required_fields_exclude_observability` — *"POST /api/onboard 400-on-missing set is exactly {collection,key,group,host}; guards D.1 over HTTP."*
- `test_onboard_succeeds_when_suggest_telemetry_raises` — *"with suggest monkeypatched to raise, /api/onboard returns the UNCHANGED `{rc,applied,output,ok}` (byte-equal output); guards D.3/D.6. NB: /api/onboard never sets an `error` key — assert the unchanged tuple, not a phantom error key."*
- `test_onboard_and_observe_are_separate_routes` — *"a forced 500 in the observe handler leaves /api/onboard unaffected; guards C1."*
- `test_neither_apply_runs_ansible_or_docker` — *"neither onboard-apply nor observe-promote invokes gitio._run with ansible-playbook/docker; guards the propose-not-enact boundary."*

**`tests/e2e/test_observe_flow.py`:** `test_observe_dialog_opens_for_onboarded_key_without_search`, `test_onboard_completes_with_nudge_ignored`, `test_onboard_apply_does_not_trigger_observe`, `test_onboard_failure_renders_no_success_nudge`.

**(Deferred, lands with the service root):** `test_service_onboard_writes_no_observability`, `test_observe_dialog_404s_on_unonboarded_service_key`, `test_telemetry_suggest_service_offers_exportarr_only_from_kind`.

---

## 1. Propose-then-promote, minimally (because it does not exist yet)

`grep` of `api/` for any two-phase promotion primitive finds none; the only existing gate is the single `apply: bool` on `OnboardIn` (`api/routes/onboard.py:34,64`) and the GUI's `field-apply` checkbox. The self-review (security lens, blocker) and the api-architecture notes confirm propose-then-promote is **design-only**. We build the smallest version that satisfies the doctrine without inventing infrastructure.

**Design decision — propose-then-promote is a *stateless, plan-hash-bound* two call pattern, not a server-side session:**

- **Propose** = the existing dry-run shape, extended to return a **`plan_token`**: a deterministic digest over the *exact bytes the promote will write*. The server stores **nothing**.
- **Promote** = a second call that re-derives the plan **from the same inputs**, recomputes the digest, and **refuses (409) if it differs** from the `plan_token` the client echoes back. This guarantees "what you saw is what you apply" without server state, race windows, or a TTL store. It also makes the audit line carry the `plan_token`, correlating proposal→promotion through the free-text `detail` field (the audit format needs **no new column** — `api/audit.py:14` `_FIELDS` is fixed; we put the token in `detail`, per the grounding note).

**New file:** `scripts/kontroll/service/promote.py`

```python
"""propose-then-promote — a stateless plan-hash gate for privileged mutations.

A PROPOSE call returns plan_token = sha256 over the canonical bytes the apply would write.
A PROMOTE call re-derives the plan from the SAME inputs, recomputes the token, and refuses
(conflict) if it differs — so "what you saw is what you apply", with NO server-side state,
NO TTL, NO race window. The token co-appears in the audit detail, correlating the two phases.
Pure: hashing + canonicalization only; no I/O, no writes.
"""
import hashlib
import json


def plan_token(*parts: str) -> str:
    """Deterministic digest over the ordered, canonicalized text blocks a promote will write
    (e.g. the metrics-block YAML, the regenerated target bodies). Stable across processes:
    JSON-encode the tuple with sort_keys so dict ordering can never perturb the hash."""
    blob = json.dumps([p for p in parts], ensure_ascii=False, sort_keys=True)
    return "plan-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def verify_token(presented: str, *parts: str) -> bool:
    """True iff the presented token matches a freshly-recomputed token over the same parts.
    A False return is a 409 at the route — the working tree or inputs changed since propose."""
    import hmac
    return bool(presented) and hmac.compare_digest(presented, plan_token(*parts))
```

This is the **minimal** primitive the security blocker asked for: it is built here, it is pure/testable, and it adds zero trust boundary. Every observability mutation route below uses it.

---

## 2. The telemetry-method picker must be *filtered* — `applicable_methods`

The task requires the picker be **filtered to applicable methods for the detected class**. The registry descriptors carry a `kind` (`host-agent | proxy-exporter | push`) and — per the modularity dimension — an `applies_when` predicate block (reusing the existing `predicate.eval_pred` kinds so no engine change). The GUI must not offer `host_node` for a `cliconf`-only switch, nor `snmp` for a Linux box that has no SNMP.

**New file:** `scripts/kontroll/service/telemetry.py`

```python
"""telemetry service — the read-only chooser/suggester behind the observability opt-in.

list_methods()            -> the full registry (label + kind + params schema), for the picker.
applicable_methods(facts) -> the SUBSET whose `applies_when` predicate fits a class's facts,
                             each annotated {state: yes|maybe} so the GUI can default the best
                             fit and grey out the rest. Three-valued: a deep-only signal reads
                             'maybe', NEVER a wrong 'no' (shallow⊑deep, the engine's invariant).
suggest_dashboards(method)-> the curated default gnet ids for that method (host_node->1860,
                             pve->10347, snmp-><switch board>), as the picker's default tier.

Pure: reads the loaded registry + folds predicates; no writes, no network. The live grafana.com
search tier is DEFERRED (see §8) — this ships the curated map only.
"""
from kontroll import predicate


def list_methods(methods):
    """[{name,label,kind,params}] for every telemetry/<method>.yml — the unfiltered picker source."""
    return [{"name": m["name"], "label": m.get("label", m["name"]), "kind": m["kind"],
             "params": m.get("params", {})} for m in methods]


def applicable_methods(facts, methods):
    """The methods whose `applies_when` block fits `facts`, each with a three-valued fit state.
    A method with no `applies_when` is universally applicable (state 'yes'). Highest-confidence
    fit first; 'maybe' (a deep-only signal at shallow depth) is kept, never demoted to 'no'."""
    out = []
    for m in methods:
        pred = m.get("applies_when")
        if pred is None:
            out.append({"name": m["name"], "label": m.get("label", m["name"]),
                        "kind": m["kind"], "params": m.get("params", {}), "fit": "yes"})
            continue
        state = predicate.eval_pred(pred, facts)           # True / False / None
        if state is False:
            continue                                       # not applicable — omit from the picker
        out.append({"name": m["name"], "label": m.get("label", m["name"]), "kind": m["kind"],
                    "params": m.get("params", {}), "fit": "yes" if state else "maybe"})
    return out


def suggest_dashboards(method):
    """The curated default dashboards for a method (the descriptor's `dashboards:` field, if any).
    Empty list degrades the GUI to 'manual gnet entry' — honest, not a fake suggestion."""
    return list(method.get("dashboards") or [])
```

`applicable_methods` is what the picker is **filtered by**: a Linux `docker_host` sees `host_node` (`fit: yes`) and nothing agent-less; a `cisco_ios` switch sees `snmp`/`blackbox` and not `host_node`. The HONEST caveat the self-review demanded: for the **service root** (radarr/exportarr) there are no probe facts, so `applicable_methods` is fed the **OpenAPI-derived** facts (`/metrics` endpoint detected → `exportarr` fits) — but that path is **deferred to §9**; for the device root this is collection facts.

---

## 3. New API routes — `api/routes/observe.py` (the drop-in)

Following the exact router-drop-in doctrine (`api/main.py:51` include loop) and the privileged-route template (`api/routes/refresh.py`, `api/routes/onboard.py`).

**New file:** `api/routes/observe.py`

```python
"""Observability routes — the GUI-actuatable per-device/service observability brick.

ROUTES
  GET  /telemetry/methods                 inquiry  — the registry (picker source)
  GET  /telemetry/suggest/{collection}    inquiry  — applicable methods + dashboards for a class
  POST /observe                           privileged — PROPOSE (default) / PROMOTE (apply:true)

BOUNDARY (docs/api-architecture.md §8, this design §0): /observe writes DATA only — it injects
the module's metrics:/dashboards: block, regenerates the Prometheus target lockfile (gen-
observability.py, pure stdlib, no lab), and commits it (audited, run_id-correlated). It does NOT
install agents, provision exporters, render secrets, or reload Prometheus/Grafana — that ENACT
step is a Semaphore task / deploy-stack, exactly like onboard's bootstrap (the API does not run
Ansible). The response carries the deterministic next-step commands for the operator/Semaphore.

Propose-then-promote: PROPOSE returns a plan_token over the exact bytes apply will write; PROMOTE
echoes it back and is refused (409) if the working tree drifted since (scripts/kontroll/service/
promote.py). Privileged + fail-closed: no token => 503; audit BEFORE mutate => 503 if unwritable;
the chosen method + dashboard + plan_token are audited, NEVER a credential.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

from api.auth import Principal, audit_action, require_token
from api.deps import Catalog, get_catalog
from kontroll import gitio
from kontroll.service.observe import (apply_observe_plan, build_observe_plan,
                                      observe_plan_token)
from kontroll.service.telemetry import applicable_methods, list_methods

router = APIRouter(tags=["privileged"])   # NOTE: see §6 — /telemetry/* are split to an inquiry router


class ObserveIn(BaseModel):
    """Enable observability for an already-onboarded device class. `method` is a telemetry/<name>.yml
    key (validated against the registry); `params` are the method's allow-listed params (e.g.
    {module: if_mib} for snmp); `dashboards` is a list of grafana.com gnet ids to declare. NO
    credential ever appears here — exporter secrets flow via the SOPS domain the method names, at
    deploy/Semaphore enact time, never through this body."""
    key: str                                  # the device-class key (modules/<key>/module.yml must exist)
    method: str                               # telemetry method name
    params: dict = {}
    dashboards: list[int] = []                # gnet ids to add to the module's dashboards:
    apply: bool = False                       # False = PROPOSE; True = PROMOTE
    plan_token: Optional[str] = None          # required when apply=true (the proposal's token)
    push: bool = False

    @field_validator("key", "method")
    @classmethod
    def _slug(cls, v: str) -> str:
        """Reject anything but a safe slug — guards the path/YAML the generator writes (config-
        injection: the onboarding input must never break out of the module-block structure)."""
        import re
        if not re.fullmatch(r"[a-z0-9_]+", v or ""):
            raise ValueError("must be a lowercase slug [a-z0-9_]")
        return v


def _observe_view(plan: dict) -> dict:
    """Client-safe plan: the metrics/dashboards block to be written, the regenerated target paths +
    bodies (the diff the operator confirms), the enact commands, and the plan_token — never a secret
    (the method's secret_domain is named, its value is not present anywhere in the plan)."""
    return {"key": plan["key"], "method": plan["method"], "kind": plan["kind"],
            "metrics_block": plan["metrics_block"], "dashboards": plan["dashboards"],
            "module_path": plan["mod_path"], "target_files": plan["target_files"],
            "secret_domain": plan["secret_domain"],          # NAME only (None for host-agent reusing existing)
            "enact": plan["enact_commands"], "plan_token": plan["plan_token"]}


@router.post("/observe", summary="Enable observability for a device class (propose, or promote — audited)")
def observe(body: ObserveIn, request: Request,
            principal: Principal = Depends(require_token),
            cat: Catalog = Depends(get_catalog)) -> dict:
    """PROPOSE (default): compute the metrics:/dashboards: block + the regenerated target lockfiles
    and return them + a plan_token; writes NOTHING. PROMOTE (apply:true + matching plan_token): write
    the module block, regenerate targets, commit + (optional) push the local canonical — audited. The
    ENACT step (install agent / provision exporter / fetch dashboard / reload) is a Semaphore task, in
    the response's `enact` field, NOT run here. 404 if the class isn't onboarded; 422 unknown method;
    409 if the plan drifted since propose."""
    plan = build_observe_plan(body.key, body.method, params=body.params, dashboards=body.dashboards,
                              methods=cat.telemetry)
    if plan["error"] == "no_module":
        raise HTTPException(status_code=404, detail="onboard the device class first: %s" % body.key)
    if plan["error"] == "unknown_method":
        raise HTTPException(status_code=422, detail="no telemetry method named %r" % body.method)
    if plan["error"] == "bad_params":
        raise HTTPException(status_code=422, detail="param not allowed for method %r: %s"
                            % (body.method, plan["detail"]))
    if not body.apply:
        return {"applied": False, "plan": _observe_view(plan), "run_id": principal.run_id}

    if not observe_plan_token(plan, presented=body.plan_token):
        raise HTTPException(status_code=409,
                            detail="plan changed since propose; re-propose and re-confirm")
    audit_action(request, principal, "observe-promote",
                 "key=%s method=%s kind=%s dashboards=%s secret_domain=%s plan=%s push=%s" % (
                     body.key, body.method, plan["kind"],
                     ",".join(str(d) for d in body.dashboards) or "none",
                     plan["secret_domain"] or "none", plan["plan_token"], body.push))
    applied = apply_observe_plan(plan)
    if not applied["changed"]:
        return {"applied": True, "changed": False, "committed": False,
                "run_id": principal.run_id, "enact": plan["enact_commands"],
                "note": "already declared; nothing to commit"}
    git = gitio.commit_and_push(
        applied["paths"],
        ["feat(observe): %s -> %s telemetry" % (body.key, body.method),
         "Enabled %s observability via the kontroll API (run_id %s). Targets regenerated; "
         "agent/exporter bring-up is a Semaphore/deploy-stack enact step (the API runs no plays)."
         % (body.method, principal.run_id)],
        push_origin=body.push)
    if not git["committed"]:
        raise HTTPException(status_code=500, detail="commit failed; nothing pushed")
    return {"applied": True, "changed": True, "committed": True,
            "pushed_canonical": git["pushed_canonical"], "pushed_origin": git["pushed_origin"],
            "run_id": principal.run_id, "enact": plan["enact_commands"],
            "next": "run the `enact` commands (or trigger the Semaphore observability task)"}
```

**New file:** `api/routes/telemetry.py` (the **inquiry** half — split so it is *not* privileged; methods/suggest are read-only and must not consume the privileged budget):

```python
"""Telemetry inquiry routes — read-only, no token. GET /telemetry/methods + /telemetry/suggest/{c}.
The picker's data source. Mirrors api/routes/classify.py (inquiry, 404 if the class isn't probeable)."""
from fastapi import APIRouter, Depends, HTTPException

from api.deps import Catalog, get_catalog
from api.models import TelemetryMethod, TelemetrySuggestion
from kontroll.service.classify import _deep_facts_or_none   # reuse classify's probe-or-404 helper
from kontroll.service.telemetry import applicable_methods, list_methods, suggest_dashboards

router = APIRouter(tags=["inquiry"])


@router.get("/telemetry/methods", response_model=list[TelemetryMethod],
            summary="List the telemetry-method registry (the picker source)")
def methods(cat: Catalog = Depends(get_catalog)) -> list:
    return list_methods(cat.telemetry)


@router.get("/telemetry/suggest/{collection}", response_model=TelemetrySuggestion,
            summary="Applicable telemetry methods + dashboards for a collection/class")
def suggest(collection: str, cat: Catalog = Depends(get_catalog)) -> dict:
    facts = _deep_facts_or_none(collection)
    if facts is None:
        raise HTTPException(status_code=404, detail="not installed locally: %s" % collection)
    methods = applicable_methods(facts, cat.telemetry)
    return {"collection": collection, "methods": methods,
            "dashboards": {m["name"]: suggest_dashboards(
                next(d for d in cat.telemetry if d["name"] == m["name"])) for m in methods}}
```

**New pydantic models** in `api/models.py` (typed-contract convention; `Record.capabilities` already flows a future `telemetry` cell with no change):

```python
class TelemetryMethod(BaseModel):
    """One telemetry method as the picker sees it."""
    name: str
    label: str
    kind: str                       # host-agent | proxy-exporter | push
    params: dict = {}
    fit: Optional[str] = None       # 'yes' | 'maybe' when returned filtered by /suggest

class TelemetrySuggestion(BaseModel):
    """Applicable methods + their curated dashboards for one class."""
    collection: str
    methods: list[TelemetryMethod]
    dashboards: dict[str, list[int]]

class ObservePlan(BaseModel):
    """The client-safe /observe plan view — the diff the operator confirms + the plan_token."""
    key: str
    method: str
    kind: str
    metrics_block: str              # rendered YAML to be inserted into module.yml
    dashboards: list[int]
    module_path: str
    target_files: dict[str, str]    # repo-rel path -> regenerated body (the lockfile diff)
    secret_domain: Optional[str]    # the SOPS domain NAME the exporter needs (None for host-agent)
    enact: list[str]                # the operator/Semaphore next-step commands
    plan_token: str
```

---

## 4. The service layer — `scripts/kontroll/service/observe.py` (what PROMOTE actuates)

This is the pure-plan / impure-apply split (the onboard pattern, `onboard.py:24/89`). It is the **only** code that writes observability data. It deliberately stops at the data write + target regen; the enact commands are **returned as strings**, not executed.

**New file:** `scripts/kontroll/service/observe.py`

```python
"""observe domain — enable per-device/service observability as a DATA-WRITE proposal.

build_observe_plan  — PURE. Reads the module + the chosen telemetry/<method>.yml, renders the
                      metrics:/dashboards: block to inject, regenerates the Prometheus target
                      lockfile bodies (in memory) so the plan shows the exact diff, computes the
                      plan_token, and assembles the ENACT command list. No writes.
apply_observe_plan  — IMPURE. Inserts the metrics:/dashboards: block into modules/<key>/module.yml
                      (idempotent — a no-op if already declared), writes the regenerated target
                      files, and returns {changed, paths to git-add}. Does NOT install agents,
                      provision exporters, render secrets, fetch dashboards, or reload Prometheus —
                      the ENACT step (the returned commands) is a Semaphore/deploy-stack operator
                      action (the service layer runs no plays, no docker, no network).
observe_plan_token  — re-derive + constant-time compare the plan_token (propose-then-promote gate).

Exporter secrets NEVER pass through here: the method descriptor NAMES its secret_domain; the value
is rendered no_log into .env by deploy-stack at enact time, exactly as PVE_EXPORTER_* is today.
"""
import os

import yaml

from kontroll import gitio, paths
from kontroll.service.promote import plan_token, verify_token

# gen-observability is import-light (yaml + stdlib) so we reuse its renderer for the lockfile diff.
import importlib.util
_GEN = os.path.join(paths.ROOT, "scripts", "gen-observability.py")
_spec = importlib.util.spec_from_file_location("gen_observability", _GEN)
gen = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(gen)


def _method(name, methods):
    return next((m for m in methods if m["name"] == name), None)


def build_observe_plan(key, method_name, params=None, dashboards=None, methods=None):
    """Compute the observability PLAN for device-class `key` using telemetry method `method_name`.
    Returns {error: 'no_module'|'unknown_method'|'bad_params'|None, ...}. On success: the metrics
    block YAML to insert, the regenerated target-file bodies, the secret_domain NAME, the enact
    commands, and the plan_token."""
    params = params or {}
    dashboards = dashboards or []
    mod_path = os.path.join("modules", key, "module.yml")
    abs_mod = os.path.join(paths.ROOT, mod_path)
    if not os.path.exists(abs_mod):
        return {"error": "no_module"}
    m = _method(method_name, methods or [])
    if m is None:
        return {"error": "unknown_method"}
    # CONFIG-INJECTION GUARD: params are a CLOSED allow-list declared by the method descriptor.
    allowed = set((m.get("params") or {}).keys())
    bad = [k for k in params if k not in allowed]
    if bad:
        return {"error": "bad_params", "detail": ",".join(sorted(bad))}

    module = yaml.safe_load(open(abs_mod, encoding="utf-8")) or {}
    # The metrics entry references the method BY NAME (the Layer-1 schema): {method, [params]}.
    entry = {"method": method_name}
    if params:
        entry["params"] = params
    existing = module.get("metrics") or []
    metrics_block = list(existing) + ([entry] if entry not in existing else [])
    dash_block = list(module.get("dashboards") or [])
    for gnet in dashboards:
        if not any(d.get("gnet") == gnet for d in dash_block):
            dash_block.append({"gnet": gnet, "name": "%s-%s" % (key, gnet)})

    metrics_yaml = yaml.safe_dump({"metrics": metrics_block}, sort_keys=False, width=4096)
    # Regenerate the target lockfiles for THIS module so the plan shows the exact diff. Reuses the
    # generator's join over the (proposed) module + the real inventory — safe_dump-escaped (the
    # generator's renderer is the single source of truth for the on-disk bytes).
    target_files = _regen_targets_for(key, module, metrics_block)

    kind = m["kind"]
    secret_domain = m.get("secret_domain")               # None for host-agent reusing an existing token
    enact = _enact_commands(key, m, module)
    token = plan_token(metrics_yaml, yaml.safe_dump({"dashboards": dash_block}, sort_keys=False),
                       *[target_files[p] for p in sorted(target_files)])

    return {"error": None, "key": key, "method": method_name, "kind": kind,
            "metrics_block": metrics_yaml, "metrics_list": metrics_block, "dashboards": dashboards,
            "dash_list": dash_block, "mod_path": mod_path, "target_files": target_files,
            "secret_domain": secret_domain, "enact_commands": enact, "plan_token": token}


def _regen_targets_for(key, module, metrics_block):
    """{repo-rel target path: body} the regenerated lockfile(s) for this class — via the generator's
    own _entries/_render so the bytes are byte-identical to what gen-observability.py would write."""
    inv = gen._load("instance/inventory/hosts.yml")
    hosts = gen._hosts_in_group(inv, module.get("inventory_group"))
    out = {}
    for met in metrics_block:
        rows = gen._entries(met, module.get("inventory_group"), hosts)   # Layer-1 dispatcher
        if rows:
            job = met.get("job") or met["method"]      # Layer-1: job comes from the method descriptor
            out["prometheus/targets/%s/%s.generated.yml" % (job, key)] = gen._render(rows)
    return out


def _enact_commands(key, method, module):
    """The deterministic next-step commands the operator/Semaphore runs to BRING the telemetry up —
    returned as strings, NEVER executed here. host-agent => install the agent; proxy-exporter =>
    deploy-stack provisions the exporter container + renders its secret; then fetch the dashboard +
    reload. These mirror the existing operator playbooks/scripts exactly."""
    cmds = ["python3 scripts/gen-observability.py    # (already done by promote; re-run is a no-op)"]
    if method["kind"] == "host-agent":
        role = method.get("agent_role", "node_exporter")
        grp = module.get("inventory_group", "")
        cmds.append("ansible-playbook ansible/playbooks/install-%s.yml -e target=%s --check --diff"
                    % (role.replace("_", "-"), grp))
        cmds.append("ansible-playbook ansible/playbooks/install-%s.yml -e target=%s" % (role.replace("_", "-"), grp))
    elif method["kind"] == "proxy-exporter":
        cmds.append("# provision the exporter container + render its secret into .env, then reload:")
        cmds.append("ansible-playbook ansible/playbooks/deploy-stack.yml -e 'stack_services=[prometheus,grafana]'")
    cmds.append("python3 scripts/fetch-dashboards.py    # fetch + provision the curated dashboard(s)")
    # NB (item F): the real builder emits the reload as a kind:'semaphore' step (the reload-observability task
    # POSTs /-/reload — no docker socket), not this kill -HUP sketch; the host-agent install is a Semaphore task too.
    cmds.append("ansible-playbook ansible/playbooks/reload-observability.yml  # reload scrape config (HTTP /-/reload)")
    return cmds


def observe_plan_token(plan, presented):
    """Propose-then-promote gate: True iff `presented` matches a freshly-recomputed token."""
    parts = [plan["metrics_block"],
             yaml.safe_dump({"dashboards": plan["dash_list"]}, sort_keys=False),
             *[plan["target_files"][p] for p in sorted(plan["target_files"])]]
    return verify_token(presented, *parts)


def apply_observe_plan(plan):
    """Insert the metrics:/dashboards: block into module.yml (idempotent) and write the regenerated
    target lockfile(s). Returns {changed, paths}. NO agent install / exporter / dashboard fetch /
    reload — those are the returned enact commands, an operator/Semaphore step."""
    abs_mod = os.path.join(paths.ROOT, plan["mod_path"])
    module = yaml.safe_load(open(abs_mod, encoding="utf-8")) or {}
    changed = False
    if (module.get("metrics") or []) != plan["metrics_list"]:
        module["metrics"] = plan["metrics_list"]; changed = True
    if (module.get("dashboards") or []) != plan["dash_list"]:
        module["dashboards"] = plan["dash_list"]; changed = True
    if changed:
        with open(abs_mod, "w", encoding="utf-8", newline="\n") as fh:
            yaml.safe_dump(module, fh, sort_keys=False, allow_unicode=True, width=4096)
    paths_changed = [plan["mod_path"]]
    for rel, body in plan["target_files"].items():
        ap = os.path.join(paths.ROOT, rel)
        os.makedirs(os.path.dirname(ap), exist_ok=True)
        if not os.path.exists(ap) or open(ap, encoding="utf-8").read() != body:
            with open(ap, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(body)
            changed = True
        paths_changed.append(rel)
    return {"changed": changed, "paths": paths_changed}
```

**What `/observe` apply ACTUATES, exactly (the task's required enumeration):**

| Step | Who actuates | Where |
|---|---|---|
| 1. Write the module `metrics:`/`dashboards:` block | **PROMOTE** (in-process, audited) | `apply_observe_plan` → `modules/<key>/module.yml` |
| 2. Regenerate the Prometheus target lockfile | **PROMOTE** (in-process, `become:false`, no lab) | `apply_observe_plan` → `prometheus/targets/<job>/<key>.generated.yml` |
| 3. Commit (+ optional push) the local canonical | **PROMOTE** (in-process, audited, run_id in commit body) | `gitio.commit_and_push` |
| 4. **Install the host agent** *(host-agent kind)* | **ENACT — operator/Semaphore** | `ansible/playbooks/install-node-exporter.yml -e target=<grp>` (already designed for "the onboard --observability opt-in", per its header) |
| 5. **Provision the exporter + render its secret** *(proxy-exporter kind)* | **ENACT — operator/`deploy-stack`** | `deploy-stack.yml` renders `.env` `no_log` + `docker compose up` |
| 6. **Fetch the dashboard JSON** (online) | **ENACT — operator** | `scripts/fetch-dashboards.py` |
| 7. **Reload Prometheus/Grafana** | **ENACT — Semaphore task** (Tier-1, item F) | the `reload-observability` task POSTs `/-/reload` (HTTP, no docker socket; Grafana auto-provisions) |

Steps 1–3 are the GUI brick. Steps 4–7 are returned as the `enact` command list and run through the **existing** delegated path. This is the boundary, made concrete.

---

## 5. GUI surface — the standalone observability dialog (`gui/templates/index.html` + `gui/app.py`)

> **Revised — standalone-dialog design of record.** The observability opt-in is **no longer a section of the onboard card**; it is a **standalone, anytime-invokable, per-device/service dialog** keyed on an already-onboarded device-class `key`. **Stays byte-identical:** `/observe`, `/telemetry/*`, propose-then-promote (`promote.py`), `applicable_methods`/`suggest_dashboards`, the `observe.py` service layer, the `galaxy.py observe` CLI, and the PROPOSE/ENACT boundary (§0, §4 table). **Changes:** the GUI packaging (a standalone `observeDialog(key, {root, collection})` builder, not a sub-section of `onboardForm()`), three entry points (none at create), the dialog's **own** promote control, and a render-only post-onboard nudge. **Anti-rebuild guard:** the dialog calls the *same* functions/routes — no `build_observe_plan_for_dialog`, no second telemetry loader, no dialog-specific promote primitive. The GUI shells to `galaxy.py observe` via `run_galaxy` (the same CLI-actuation model as `/api/onboard`, `gui/app.py:111`); the GUI→API convergence stays deferred (§10).

### 5.0 What this is

The data write the dialog proposes (`metrics:`/`dashboards:` into `modules/<key>/module.yml` + regenerated target lockfiles) is a **strictly-after, optional follow-on** to onboarding — `/observe` 404s `no_module` when the class isn't onboarded (§3 lines 219-222), which is exactly what makes observability un-gating (§0.1 INVARIANT D). It is not a section of the onboard card and not invoked at create-time.

### 5.1 ONE surface for both roots (decision: ONE dialog, root-agnostic)

One surface for the device root and the (deferred) service root. The operator's mental model ("this should be monitored — pick how") and the five-stage flow are identical; `/observe`, `applicable_methods`, and `plan_token` are already keyed on `key`, not root (§9). The only per-root divergence — which facts feed `applicable_methods` (collection deep-probe vs OpenAPI spec) — is absorbed by **one query-param branch in one inquiry route** (§5.6), never an `if root=='service'` branch in the GUI. A `kind · key` context chip differentiates `device · cisco_ios` from `service · radarr`; the filtered picker shows the right methods automatically. Two dialogs would duplicate the flow, testids, and e2e for zero usability gain and a guaranteed drift hazard.

### 5.2 Entry points (three; none at create)

The dialog targets an already-onboarded `key` (`modules/<key>/module.yml` must exist). All three converge on `openObserveDialog(key, {root, collection})`.

- **EP-1 — card-local "Set up monitoring" button.** A **visually subordinate** (ghost/secondary, never co-equal with the primary Onboard CTA) sibling of `onboard-toggle`, `data-testid="observe-toggle"`. On open it calls `GET /api/telemetry-suggest`; a 404 renders the honest `observe-not-onboarded` state ("Onboard this device first — monitoring is set up afterwards"), not an empty picker.
- **EP-2 — the post-onboard nudge (render-only, non-binding).** See §5.5. Fires on a *successful* onboard, renders a dismissible affordance beside the onboard output that *launches* the dialog. Never a checkbox, never auto-open, never blocking.
- **EP-3 — anytime "by key" entry.** A top-level affordance (`observe-by-key`) for a class onboarded earlier, **backed by a key picker, not a bare text box**: a trivial inquiry endpoint `GET /api/onboarded` enumerates `instance/fleet.yml` `enabled_modules` (+ each module's `kind`/`collection`) so the operator *recognizes* a class rather than recalling a slug. If `GET /api/onboarded` is descoped this iteration, EP-3 ships as a typo-tolerant key input (on 404, "did you mean `<closest enabled key>`?") and is **not** called the "primary" path — EP-1/EP-2 cover the common cases, and a later inventory view adds the durable entry.

### 5.3 The guided flow (propose → promote → enact)

Single panel, stages revealed top-to-bottom; testid namespace `observe-dialog`; header chip `observe-context` shows `kind · key`.

- **Stage 0 — detect (`observe-loading`).** `GET /api/telemetry-suggest` resolves applicable methods, their `fit` (yes|maybe), and curated dashboards in one round-trip.
- **Stage 1 — method picker (`observe-method`).** Only applicable methods (`fit:'no'` omitted). First `fit:'yes'` pre-selected. `maybe` listed, suffixed "(deep-probe to confirm)", de-emphasized, never hidden. `observe-method-hint` states the `kind` in plain words (proxy-exporter = "agent-less — a control-node exporter scrapes it; nothing installed on the device"; host-agent = "installs node_exporter on the host").
- **Stage 2 — params (`observe-params`).** One labeled input per declared param (closed allow-list), defaulted from the descriptor; where the descriptor enumerates valid *values* (e.g. snmp `module`), render a select / "common: …" hint; a raw-JSON escape hatch behind an "advanced" toggle. Absent when the method has no params.
- **Stage 3 — dashboards (`observe-dashboards`).** Defaulted from curated `dashboards[method]` (§8), shown as named chips ("Node Exporter Full (1860) ×"); "+ add by grafana.com id" is the manual tier; honest empty state when none curated. The live grafana.com tier stays deferred (§8).
- **Stage 4 — PROPOSE (`observe-propose`, "Preview the change").** POSTs `/observe` with `apply:false`. `observe-plan` renders three labeled blocks: (1) "Will write to `modules/<key>/module.yml`", (2) "Will regenerate Prometheus targets" (the lockfile diff), (3) "You will run next (not done automatically)" — the `enact[]` commands. Apply activates only after a successful preview (the UI enforces the two-call gate).
- **Stage 5 — PROMOTE (`observe-apply`).** The dialog's **own** dedicated control (NOT the onboard card's `field-apply`), default unchecked. Re-POSTs `/observe` with `apply:true` + the previewed `plan_token`; the server re-derives and refuses **409** on drift (§1). The button is labeled to make the PROPOSE/ENACT gap impossible to miss: **"Commit config (won't start monitoring yet)"** with an always-visible subline "writes + commits the config; you still run the steps below to go live."
- **Stage 6 — enact handoff (terminal, `observe-enact`).** The `enact[]` list with "Copy all", framed as a deliberate handoff ("Hand these to whoever runs deploy-stack / Semaphore"), the `run_id` as a copyable correlation token, and a "after running, this target appears in Prometheus" verification hint. Where the master design contemplates triggering the same Semaphore task (§0 / line 23), surface it as an optional button or say explicitly it's deferred. Status line: "Monitoring config is committed (run_id `<id>`). It is not live until you run the steps above."

**Best-fit fast path:** open → (method pre-selected, params defaulted, dashboard defaulted) → Preview → read diff → Commit → copy enact.

### 5.4 States (every non-happy path)

| State | testid | What the operator sees | Source |
|---|---|---|---|
| Loading | `observe-loading` | "Checking what's scrapable…" | the `/telemetry/suggest` round-trip |
| No applicable methods | `observe-none` | "Nothing here looks scrapable from what we can detect. If this is a Linux host running node_exporter, you can still add the host agent manually." + manual `host_node` escape | the vector is a *suggester*; `host_node` is operator-declared (`telemetry-capability-vector.md:13-35`) |
| **Service deferred** | `observe-service-deferred` | "Service monitoring isn't available yet (it ships with the service root). This dialog currently supports Galaxy-collection devices." | shown when the key is `kind: service`/collection-less while `build_service_plan` + the service facts feed are unbuilt — NOT the generic `observe-none` |
| Already monitored | `observe-already` | "✓ Already monitored via **snmp** (declared in `module.yml`). You can **add** another method or dashboard here; to change or remove the existing one, edit `module.yml` / use the CLI." | `classify.declared_metrics_methods`. **ADD-ONLY in the MVP** — `apply_observe_plan` is an idempotent insert, not a replace, so the dialog never offers a change/remove affordance the apply path can't honor |
| Not onboarded | `observe-not-onboarded` | "Onboard this device first — monitoring is set up afterwards. [Onboard]" | the 404 from `/telemetry/suggest`; points forward, never blocks |
| Transport error | `observe-error` | "Couldn't check this device right now — try again." | the `fetch` threw |
| Bad params | inline on `observe-params` | "snmp doesn't accept 'foo' — allowed: module, auth." | the closed allow-list rejection, surfaced per-field |
| Promote 409 (drift) | `observe-drift` | "The repository changed since you previewed this. Re-preview before applying." Apply disables, Preview re-highlights. | `verify_token` mismatch (§1) — the anti-drift contract made visible |

### 5.5 The post-onboard nudge (render-only, non-binding, zero new I/O)

Discoverability without coupling. Sourced by `suggest_telemetry` (the read-only capability vector, `telemetry-capability-vector.md:179`; writes nothing, never auto-suggests `host_node` as primary).

- **Success gating (the critical correctness point).** The GUI's `/api/onboard` currently returns `applied: bool(d.get("apply"))` — the *request echo of the apply checkbox*, NOT a success signal (`gui/app.py:190`); `rc` can be non-zero while `applied:true`. So add a real `ok = (rc == 0)` boolean to the `/api/onboard` response, and the JS gates the nudge **and** the "✓ Onboarded" copy on `r.ok === true`, never on `applied`. An onboard returning `rc!=0` with apply ticked renders **no** success-framed nudge.
- **Zero new blocking I/O.** The nudge must NOT re-probe on the onboard response path (the HARD CONSTRAINT forbids observability *delaying* onboarding). The onboard shell-out's `deep_probe` result lives in the child process, unavailable to the parent — so the nudge renders as a *static* "set up monitoring?" affordance; any candidate-awareness fires as a **separate async fetch after** the response returns, never synchronously on the protected path.
- **Render.** On `ok===true`, render a dismissible `observe-nudge` chip + `observe-nudge-dismiss` beside the output pane. Observational copy ("This looks scrapable — set up monitoring?"), not imperative. The chip *launches* `openObserveDialog(key, {root, collection})`, never an inline control.
- **Dismissal persists across reloads.** Cards are rebuilt on every `/api/search` (`index.html:73`, the only navigation), so per-session dismissal would re-nag. Persist dismissal client-side keyed by device-class key (`localStorage`). The nudge also never renders for a class already monitored (read `declared_metrics_methods`).
- **No incomplete-state badges.** An onboarded-but-unmonitored device is shown as fully successfully onboarded; the telemetry capability badge reads a fact about the class, not a to-do. **Dependency:** the telemetry badge is the deferred Phase-4 vector dimension (the card today emits only `data-cap` of `actuate|backup|bespoke`, `index.html:83-88`); if this dialog lands first, EP-2 + EP-3 are the only discoverability signals in the interim — state it.

### 5.6 Device/service parity — what feeds the SAME dialog/route

The dialog and `/observe` are root-agnostic; only the *facts feeding `applicable_methods`* differ, and that is absorbed by `/api/telemetry-suggest`, not the dialog.

- **Device root (build now):** facts from `classify._deep_facts_or_none(collection)`. EP-1 passes `collection`; EP-3 passes `key` and the route resolves the collection from `modules/<key>/module.yml`. A Linux `docker_host` sees `host_node` not `snmp`; `cisco_ios` sees `snmp`/`blackbox` not `host_node`. `host_node` stays operator-declared (the probe carries no host-OS fact).
- **Service root (deferred):** a service has no collection (`build_onboard_plan` 404s `not_installed`, `onboard.py:32-34`). Its scrape signal is the OpenAPI spec. **Verified gap:** `openapi.classify_endpoints` (`openapi.py:80-100`) has no `/metrics` detection today. The service facts feed is a bounded, separable increment: (a) `classify_endpoints` gains a `metrics: bool` signal; (b) a `service_facts(key)` producer returns a facts dict shaped for `applies_when` (`{kind:service, exposes_metrics, app, auth}`); (c) `/api/telemetry-suggest` gains a `?service=<key>` accessor. `exportarr`'s `applies_when` fits on `kind==service` alone at `fit:yes` (it queries the API — it needs no service-native `/metrics`); any direct-scrape candidate is `fit:maybe` only. The `predicate.eval_pred` engine is unchanged. No new CLI subcommand or privileged route — `galaxy.py observe <service-key>` works because the module carries `kind: service`.

The standalone open-by-key dialog also **raises** the relevance of the deferred `build_service_plan`: it is the **only viable home for service observability** (a service has no onboard card to nest an opt-in in).

### 5.7 GUI routes, CLI, and testids (stays + deltas)

The `/api/telemetry-suggest` + `/api/observe` GUI routes and the `galaxy.py observe <key>` subcommand are **unchanged** from the prior design (their bodies live in this doc's pre-revision §5c/§5d in git history). Three small additions:

- `/api/onboard` returns a new `ok = (rc == 0)` boolean (the real success signal, distinct from the apply-echoing `applied`) plus a render-only `suggest` field computed **only on success**, never by a synchronous re-probe.
- `/api/telemetry-suggest` gains a key-only resolution path and a **deferred** `?service=<key>` accessor.
- NEW `GET /api/onboarded` (inquiry) enumerates `instance/fleet.yml` `enabled_modules` (+ `kind`/`collection`) to back the EP-3 key picker.

**testids** — replace the old "within the onboard form" set with a top-level **"Standalone monitoring dialog (per device-class key)"** section in `tests/testid_reference.md` (none nested in `onboard-form`): `observe-toggle`, `observe-by-key`, `observe-nudge`, `observe-nudge-dismiss`, `observe-dialog`, `observe-context`, `observe-loading`, `observe-method`, `observe-method-option` (`data-method`/`data-kind`/`data-fit`), `observe-method-hint`, `observe-params`, `observe-dashboards`, `observe-propose`, `observe-plan`, `observe-apply`, `observe-enact`, and the §5.4 state ids (`observe-none`, `observe-service-deferred`, `observe-already`, `observe-not-onboarded`, `observe-error`, `observe-drift`). Grep-verify before commit.

### 5z. Superseded — the original onboard-card-nested opt-in (kept for diff context)

The blocks below describe the **old** packaging (the opt-in injected into `onboardForm()`). They are **superseded by §5.0–§5.7** and retained only so a reader can see exactly what moved; the route/CLI code (5c/5d) stays valid.

### 5a. HTML — added to `onboardForm()` (after the flags row, before the Run button)

```html
<!-- Observability opt-in (per-device telemetry). Revealed by the checkbox; the method picker is
     FILTERED to applicable methods for this class via /api/telemetry-suggest. data-testid SOP. -->
<div class="full row" data-testid="observe-optin">
  <label class="row"><input type="checkbox" name="observe" data-testid="field-observe"> add observability</label>
</div>
<div class="full observe-panel" data-testid="observe-panel" style="display:none">
  <div><label>telemetry method</label>
    <select name="telemetry_method" data-testid="field-telemetry-method"></select></div>
  <div data-testid="telemetry-method-hint" class="hint"></div>
  <div><label>method params (JSON, if any)</label>
    <input name="telemetry_params" data-testid="field-telemetry-params" placeholder='{"module":"if_mib"}'></div>
  <div><label>dashboard(s) — grafana.com gnet ids, comma-sep (curated suggested)</label>
    <input name="dashboards" data-testid="field-dashboards" placeholder="1860"></div>
  <div class="full"><button type="button" class="go" data-testid="observe-run">Propose observability</button>
    <span class="warn">Proposes the metrics block + target diff; promote applies the data write.
      Agent/exporter bring-up is an operator/Semaphore step (shown in the plan).</span></div>
  <pre class="out full" data-testid="observe-output" style="display:none"></pre>
</div>
```

### 5b. JS — filtered picker population + propose/promote submit

```javascript
// On reveal, fetch the FILTERED method list for this card's collection and populate the <select>.
async function loadMethods(f, collection) {
  const sel = f.querySelector('[data-testid="field-telemetry-method"]');
  const hint = f.querySelector('[data-testid="telemetry-method-hint"]');
  sel.innerHTML = '';
  let data;
  try { data = await (await fetch('/api/telemetry-suggest?collection='+encodeURIComponent(collection))).json(); }
  catch(e){ hint.textContent = 'method suggestion unavailable'; return; }
  if (data.error || !(data.methods||[]).length){ hint.textContent = data.error || 'no applicable methods'; return; }
  data.methods.forEach((m, i) => {                     // already filtered to applicable; 'maybe' greyed
    const o = E('option', null, m.label + (m.fit==='maybe' ? ' (deep-probe to confirm)' : ''));
    o.value = m.name; o.dataset.kind = m.kind; o.dataset.fit = m.fit;
    o.dataset.testid = 'telemetry-method-option'; o.dataset.method = m.name;   // addressable per option
    if (m.fit !== 'yes') o.disabled = false;           // selectable but flagged; never silently hidden
    sel.appendChild(o);
  });
  const dash = f.querySelector('[data-testid="field-dashboards"]');
  sel.onchange = () => {                                // default the curated dashboards for the pick
    const ids = (data.dashboards||{})[sel.value] || []; dash.value = ids.join(',');
    hint.textContent = 'kind: ' + (sel.selectedOptions[0]?.dataset.kind || '');
  };
  sel.onchange();
}

async function submitObserve(f, key) {
  const g = n => f.querySelector('[name='+n+']');
  let params = {}; try { params = g('telemetry_params').value ? JSON.parse(g('telemetry_params').value) : {}; }
  catch(e){ /* the server validates against the method's allow-list; a bad JSON is a clean error there */ }
  const dashboards = g('dashboards').value.split(',').map(s=>parseInt(s.trim(),10)).filter(n=>!isNaN(n));
  const body = { key, method: g('telemetry_method').value, params, dashboards,
                 apply: g('field-apply') ? g('field-apply').checked : false };  // promote rides the same apply flag
  const out = f.querySelector('[data-testid="observe-output"]'); out.style.display='block'; out.textContent='Proposing…';
  try {
    const r = await (await fetch('/api/observe',{method:'POST',headers:{'Content-Type':'application/json'},
                                  body:JSON.stringify(body)})).json();
    out.textContent = (r.error ? 'ERROR: '+r.error : r.output) + (r.rc!=null ? '\n\n(exit '+r.rc+')' : '');
  } catch(e){ out.textContent = 'request failed'; }
}
// wire the opt-in checkbox to reveal the panel + lazy-load the filtered picker
f.querySelector('[data-testid="field-observe"]').addEventListener('change', e => {
  const panel = f.querySelector('[data-testid="observe-panel"]');
  panel.style.display = e.target.checked ? 'block' : 'none';
  if (e.target.checked) loadMethods(f, rec.collection);
});
f.querySelector('[data-testid="observe-run"]').onclick = () => submitObserve(f, f.querySelector('[name=key]').value);
```

### 5c. New GUI route — `gui/app.py`

```python
@app.route("/api/telemetry-suggest")
@require_auth
def api_telemetry_suggest():
    """Applicable telemetry methods (filtered to the class's facts) + curated dashboards, in-process
    over the service layer — the picker's data source. Mirrors /api/classify."""
    coll = request.args.get("collection") or ""
    methods = _catalog.load_telemetry()                          # the Layer-1 registry loader
    from kontroll.service.telemetry import applicable_methods, suggest_dashboards
    from kontroll.service.classify import _deep_facts_or_none
    facts = _deep_facts_or_none(coll)
    if facts is None:
        return jsonify({"error": "%s not installed locally" % coll}), 404
    ms = applicable_methods(facts, methods)
    return jsonify({"collection": coll, "methods": ms,
                    "dashboards": {m["name"]: suggest_dashboards(
                        next(d for d in methods if d["name"] == m["name"])) for m in ms}})


@app.route("/api/observe", methods=["POST"])
@require_auth
def api_observe():
    """Enable observability for an onboarded class — shells to `galaxy.py observe` (the same CLI-
    actuation model as /api/onboard). PROPOSE by default; promote rides the apply flag. The chosen
    METHOD/dashboards are audited; NEVER a credential (none is accepted here — exporter secrets flow
    via SOPS at enact time)."""
    d = request.get_json(force=True) or {}
    for req in ("key", "method"):
        if not d.get(req):
            return jsonify({"error": "missing required field: %s" % req}), 400
    apply = bool(d.get("apply"))
    _audit("observe" + ("-promote" if apply else "-propose"),
           "key=%s method=%s dashboards=%s" % (d["key"], d["method"],
            ",".join(str(x) for x in (d.get("dashboards") or [])) or "none"))   # NEVER a secret
    args = ["observe", d["key"], "--method", d["method"]]
    if d.get("params"):
        import json as _json
        args += ["--params", _json.dumps(d["params"])]
    for gnet in (d.get("dashboards") or []):
        args += ["--dashboard", str(gnet)]
    if apply:
        args += ["--apply", "--commit"]
    rc, out, err = run_galaxy(args)
    _audit("observe-result", "key=%s rc=%s applied=%s" % (d["key"], rc, apply))
    return jsonify({"rc": rc, "applied": apply,
                    "output": out + (("\n[stderr]\n" + err) if err.strip() else "")})
```

### 5d. New `galaxy.py observe` subcommand (the CLI the GUI shells to + the operator's own path)

`cmd_observe(args)` builds the plan via `build_observe_plan`, prints it (the metrics block + target diff + the **enact** command list), and on `--apply --commit` runs `apply_observe_plan` + commits — **identical structure** to `cmd_onboard` (`galaxy.py:200-287`). The subparser (added beside `onboard`/`openapi` at `galaxy.py:400+`): `observe <key> --method <name> [--params <json>] [--dashboard <gnet> ...] [--apply] [--commit] [--push]`. The `--params` value is JSON parsed then validated against the method's allow-list by `build_observe_plan` (the config-injection guard). This is the **propose-then-promote** in CLI form: dry-run prints + `plan_token`; `--apply` requires nothing extra in the CLI because the CLI re-derives in one process (the token gate matters for the *two-call* API path).

### 5e. testid_reference.md rows to add (same commit; grep-verified)

New section **"Observability opt-in (per card, within the onboard form)"**:

| `data-testid` | Element |
|---|---|
| `observe-optin` | the "add observability" checkbox container |
| `field-observe` | the opt-in checkbox (reveals the panel) |
| `observe-panel` | the observability panel (assert visible/hidden) |
| `field-telemetry-method` | the telemetry-method `<select>` (filtered to applicable methods) |
| `telemetry-method-option` | one `<option>`; discriminator `data-method="<name>"`, `data-kind=`, `data-fit=` |
| `telemetry-method-hint` | the kind/fit hint line |
| `field-telemetry-params` | the method-params JSON input |
| `field-dashboards` | the gnet-ids input (defaulted from the curated suggestion) |
| `observe-run` | the "Propose observability" button |
| `observe-output` | the `<pre>` plan/result pane |

---

## 6. Privileged surface, rate-limit, and the tripwire (must-land-in-same-commit)

The new **privileged** route `/observe` must be registered and classified or `test_every_privileged_route_is_classified` (`test_api_ratelimit.py:182`) fails — it hard-asserts the set `{/refresh,/capture-exceptions,/onboard,/audit/log}`.

**Required edits (same commit):**

1. `api/main.py:51` include loop → add `observe` and `telemetry`:
   ```python
   from api.routes import classify, exceptions, health, observe, onboard, probe, refresh, search, telemetry
   for module in (health, search, probe, classify, refresh, exceptions, onboard, audit_routes, observe, telemetry):
   ```
2. `api/ratelimit.py:53` `_PRIVILEGED` → add `"/observe"` (NOT `/telemetry` — that is inquiry):
   ```python
   _PRIVILEGED = ("/refresh", "/capture-exceptions", "/onboard", "/audit", "/observe")
   ```
3. `tests/integration/test_api_ratelimit.py:182` → update the asserted set to include `/observe`; assert `/telemetry/methods` and `/telemetry/suggest/{collection}` classify as `inquiry`.
4. `api/deps.py` `Catalog` dataclass + `load_catalog()` → add a `telemetry` field loaded once from the Layer-1 registry:
   ```python
   @dataclass
   class Catalog:
       vectors: list; overrides: dict; backends: list; telemetry: list
   def load_catalog() -> Catalog:
       return Catalog(vectors=catalog.load_vectors(), overrides=catalog.load_overrides(),
                      backends=catalog.load_backends(), telemetry=catalog.load_telemetry())
   ```

**Fail-closed posture (inherited verbatim from `api/auth.py`):** `/observe` is `Depends(require_token)` → 503 when no token; `audit_action` BEFORE mutate → 503 if the audit write fails; the audit `detail` carries `method`, `kind`, `dashboards`, `secret_domain` (the **name**), `plan_token`, `push` — **never a credential** (none is accepted by `ObserveIn`; exporter secrets flow via SOPS at enact time). Rate-limit: `/observe` keys per **token digest** (privileged budget, 10/min default); `/telemetry/*` keys per IP (inquiry, 30/min).

---

## 7. Audit + log lines (run_id-correlated; action, never credential)

**API silo (fail-closed, 6-field TSV, `api/audit.py`):**
```
2026-06-14T09:31:02Z   api-token   198.51.100.7   observe-promote   20260614T093102Z-a1b2c3   key=cisco_ios method=snmp kind=proxy-exporter dashboards=1860 secret_domain=snmp_observability plan=plan-9f3a... push=False
```
The `plan-…` token in `detail` correlates the prior `observe` propose to this promote (no new audit column needed). The commit body carries the same `run_id` (`feat(observe): cisco_ios -> snmp telemetry … run_id 20260614T093102Z-a1b2c3`), so the GUI action → git commit is greppable end-to-end.

**GUI silo (best-effort, 4-field TSV, `gui/app.py`):**
```
2026-06-14T09:31:02Z   198.51.100.7   observe-propose   key=cisco_ios method=snmp dashboards=1860
2026-06-14T09:31:09Z   198.51.100.7   observe-promote   key=cisco_ios method=snmp dashboards=1860
2026-06-14T09:31:10Z   198.51.100.7   observe-result    key=cisco_ios rc=0 applied=True
```

**The enact step's run_id stitch (when the operator runs the install play):** the `enact` commands the plan returns include `-e kontroll_run_id=<the run_id>` so the Ansible-side `_log-run-id.yml` stamp matches the GUI/API audit line. The `_enact_commands` builder appends `-e kontroll_run_id=%s` to each `ansible-playbook` line using the principal's `run_id` (API path) or the GUI's minted id. This closes the correlation gap the self-review flagged.

**Honesty note (documented, not hidden):** the GUI silo is fail-**open** (swallows `OSError`, `gui/app.py:92`), unlike the API. The design states plainly: **for any state-changing observe, the audit line is written before the shell-out, and the canonical fail-closed audit is the API silo** — the GUI path is the dev/operator-convenience surface; production GUI-actuation routes through the API once it is deployed (§10).

---

## 8. Dashboard picker — curated-by-method default, manual fallback (hybrid, honest)

The picker defaults the `field-dashboards` input from `/api/telemetry-suggest`'s curated `dashboards[method]` map (e.g. `host_node→[1860]`, `pve→[10347]`). The operator may override with any gnet id (manual tier). The **live grafana.com search/suggest tier is explicitly DEFERRED** (it is a new online I/O seam with no mock point in `tests/README.md` and `fetch-dashboards.py` is single-datasource `DS_UID='prometheus'`). The curated map degrades to **manual-only** when a method has no curated dashboards — an honest empty default, never a fake suggestion. This matches `fetch-dashboards.py`'s own note: *"the onboarding live-search pick-list rides the same fetch path next."*

---

## 9. The service root (radarr/exportarr) — deferred, with the convergence point named

`build_onboard_plan` hard-requires `probe.deep_probe(collection)` and returns `not_installed` for a collection-less service (`onboard.py:32-34`), so a pure-API service **cannot** enter the device path. The observe flow converges on the **same** `/observe` route, but the *facts* feeding `applicable_methods` come from the OpenAPI spec (`openapi.classify_endpoints` extended to detect a `/metrics` path → `exportarr` fits), not a collection probe. This requires a collection-less `build_service_plan` that **does not exist** — so the service root is **deferred**. The design names the convergence (`/observe` is root-agnostic; only its `applicable_methods` input differs) but does not build the service plan here.

---

## 10. The end-state convergence (named, deferred): GUI → API, one silo

Once the privileged API is deployed (the C9-deferred enablement: token + writable clone + age key), the GUI's `/api/observe` should **call the API `/observe`** instead of shelling to `galaxy.py` — unifying on the fail-closed 6-field run_id audit and one actuation implementation. This design ships the **CLI-shell-out** GUI path now (consistent with the existing `/api/onboard`), and documents the API-call migration as the convergence, gated on the same deployment step. No code here presumes the API is deployed.

---

## 11. Tests (each with its what-and-why docstring; `check-test-docs.py` enforces)

**New: `tests/unit/test_observe_service.py`** (pure plan + apply over `tmp_repo`):

- `test_build_observe_plan_injects_metrics_block` — *"build_observe_plan returns a metrics: block referencing the chosen method by name; guards GAP (e) — onboarding never generated the observability block, so this proves the plan now does."*
- `test_build_observe_plan_404_when_class_absent` — *"a key with no modules/<key>/module.yml returns error 'no_module'; guards an observe call racing a non-onboarded class into a half-written module."*
- `test_build_observe_plan_422_unknown_method` — *"an unknown method name returns 'unknown_method' (no silent target); guards the closed-registry contract — a typo must fail loud, not emit a wrong/empty target."*
- `test_observe_params_allowlist_rejects_unknown_key` — *"a param key not in the method's declared allow-list returns 'bad_params'; guards the config-injection path the GUI opt-in opens (onboarding input must not break out of the module-block structure)."*
- `test_apply_observe_plan_is_idempotent` — *"a second apply of the same plan reports changed=False and rewrites nothing; guards the 0-changed-on-re-apply doctrine for the data write."*
- `test_apply_observe_plan_regenerates_byte_identical_targets` — *"the target bodies apply writes equal gen-observability.py's own _render output; guards that promote can never drift the lockfile from what `--check` validates."*

**New: `tests/unit/test_promote.py`:**

- `test_plan_token_is_deterministic_across_calls` — *"plan_token over the same parts is stable; guards the propose-then-promote contract — a stable proposal must yield the same token on promote."*
- `test_verify_token_rejects_drift` — *"verify_token returns False when any part changed since propose; guards 'what you saw is what you apply' — a tree mutated between propose and promote must 409, not silently apply a different plan."*

**New: `tests/unit/test_telemetry_service.py`:**

- `test_applicable_methods_filters_by_facts` — *"a Linux host's facts yield host_node and exclude snmp; a cliconf switch yields snmp and excludes host_node; guards the picker-filtering requirement — the GUI must not offer an inapplicable method."*
- `test_applicable_methods_maybe_never_wrong_no` — *"a deep-only applicability signal reads 'maybe' at shallow depth, never 'no'; preserves shallow⊑deep so the picker never hides a method that would actually fit."*

**New: `tests/integration/test_api_observe.py`** (mirrors `test_api_auth.py`'s `priv` fixture):

- `test_observe_503_when_token_unconfigured` — *"no KONTROLL_API_TOKEN ⇒ /observe is 503; guards the fail-closed posture for the new privileged route."*
- `test_observe_missing_token_401` / `test_observe_bad_token_401_and_audited` — *"…the auth battery the privileged-route template mandates."*
- `test_observe_propose_writes_nothing_and_returns_token` — *"apply:false returns the plan + a plan_token and the git seam is never called; guards propose-only by default (no mutation without explicit promote)."*
- `test_observe_promote_requires_matching_token_else_409` — *"apply:true with a stale/absent plan_token is 409 and nothing is committed; guards the propose-then-promote gate end-to-end over HTTP."*
- `test_observe_promote_commits_and_audits` — *"apply:true with a matching token writes the module + targets, commits (git mocked), and writes an `observe-promote` audit line carrying the run_id + method + plan token; guards the audited data-write."*
- `test_observe_audit_excludes_any_secret` — *"a request carrying an (accidental) secret-shaped field never appears in the audit line — only method/kind/dashboards/secret_domain-NAME/plan_token; the crown-jewel cred-exclusion property for the new route."*
- `test_observe_does_not_run_ansible_or_docker` — *"a promote never invokes gitio._run with ansible-playbook/docker; guards the boundary — the API writes data and returns enact commands, it does not run plays."*
- Update `test_every_privileged_route_is_classified` — *"…the asserted privileged set now includes /observe; /telemetry/* are inquiry — guards the new route against silently bypassing the per-token budget."*

**New: `tests/integration/test_gui_api.py` additions:**

- `test_observe_propose_never_passes_apply` — *"/api/observe without apply forwards `observe … --method …` to galaxy.py WITHOUT --apply; actuation is explicit."*
- `test_observe_audit_excludes_secrets_and_records_method` — *"the GUI audit records `observe-propose key=… method=…` but never a secret; mirrors the onboard crown-jewel for the observe surface."*

**New: `tests/e2e/test_observe_flow.py`** (data-testid only; `e2e` marker; reuses the `live_server` harness — `_fake_run_galaxy` already returns canned output for any non-`onboard` argv, but add an `observe` branch + a `_fake_load_telemetry`/`/api/telemetry-suggest` mock seam in `conftest.py`):

- `test_observe_optin_reveals_filtered_picker` — *"ticking `field-observe` reveals `observe-panel` and the `field-telemetry-method` select is populated with `telemetry-method-option`s for the class; guards the filtered-picker UX."*
- `test_observe_propose_renders_plan_without_apply` — *"with `field-apply` UNCHECKED, `observe-run` renders the metrics block + enact commands in `observe-output` and writes nothing; guards dry-run/propose by default for the observe surface."*

**`tests/e2e/conftest.py` new mock seam** (the install/exporter actuation is never reached in CI): add `_fake_run_galaxy` an `observe` branch returning a canned propose plan, and monkeypatch `_catalog.load_telemetry` to a fixed two-method registry — recorded as a new row in `tests/README.md`'s mock-seam table (`kontroll.catalog.load_telemetry` / `gui.run_galaxy` observe branch).

---

## 12. SECURITY.md — control text (a new privileged surface + new trust surface)

**C9 amendment** (the privileged-API surface control), add a bullet:

> **`POST /observe` (privileged, propose-then-promote).** Enables per-device/service observability as a **data-write only**: it injects the module `metrics:`/`dashboards:` block and regenerates the Prometheus target lockfile, then commits the local canonical. It **does not** run Ansible, provision exporters, render secrets, or reach the lab — agent/exporter bring-up is delegated to Semaphore/`deploy-stack` (the same boundary as `/onboard`'s bootstrap). Fail-closed (no token ⇒ 503; un-auditable ⇒ 503). The audit line records the **method, kind, dashboards, secret-domain NAME, and plan token — never a credential** (the route accepts none). Propose-then-promote (`scripts/kontroll/service/promote.py`) binds the promotion to a hash of the exact bytes proposed, so a drifted tree is refused (409) rather than silently applied. **Proves:** `tests/integration/test_api_observe.py` (auth battery + propose-only-by-default + 409-on-drift + cred-exclusion + `test_observe_does_not_run_ansible_or_docker`); `tests/integration/test_api_ratelimit.py::test_every_privileged_route_is_classified` (the route is in the privileged budget).

**New control (or C9 sub-bullet) for exporter secrets** (only when a `proxy-exporter` method names a *new* secret domain): *"An exporter's secret (SNMP community, *arr API key) lives in its own SOPS domain (`<method>_observability.sops.yml`) resolving to **base recipients (control + break-glass) only** — never the scoped Semaphore key, never co-located in `network`/`proxmox`. Rendered `no_log` into `.env` by `deploy-stack` at enact time; never present in the `/observe` body, plan, audit, or a committed compose fragment. **Proves:** the SOPS-encrypted check + a covering test asserting the value is absent from the audit/plan."* (This is the security-lens major; the **modularity** dimension owns the descriptor's `secret_domain` field and the `.sops.yaml` rule — this section only documents the route's no-secret guarantee.)

**Accepted-risk amendment:** *"The GUI's observe path (CLI shell-out) audits in the fail-OPEN GUI silo; for production GUI-actuation the convergence (§10) routes through the fail-closed API `/observe`. Until then, any state-changing observe is also auditable via the CLI's git commit (run_id-correlated)."*

---

## 13. Documentation-Sync rows (same commit)

| Changed | Update |
|---|---|
| New `/observe` + `/telemetry/*` privileged/inquiry surface | `docs/api-architecture.md` (route table + the actuation boundary §8 note that observe writes data, not plays); `SECURITY.md` C9 (above) |
| New GUI observability opt-in elements | `tests/testid_reference.md` (the §5e rows — grep-verified) |
| New log/audit actions (`observe-propose`/`observe-promote`/`observe-result`) | `docs/logging-architecture.md` §1 table (API + GUI audit rows) + §3 (the run_id stitch into the enact play's `-e kontroll_run_id=`) |
| New service/test files + the `load_telemetry` mock seam | `tests/README.md` (file inventory + the mock-seam table row) |
| The metrics block becomes onboard/observe-generated (no longer hand-authored) | `modules/README.md` (the `metrics:`/`dashboards:` rows note "generated by `galaxy.py observe` / `/observe`, not hand-authored") |
| Behaviour-affecting | `CHANGELOG.md [Unreleased]` — "feat(observe): GUI-actuatable per-device observability opt-in (propose-then-promote data-write; agent/exporter bring-up stays deploy-stack/Semaphore)" |
| New doc peers | reciprocal "See also" between `docs/api-architecture.md`, `SECURITY.md` C9, `docs/observability-architecture.md` (the Layer-1 doc the modularity dimension creates) |

---

## 14. Acceptance criteria

1. **Propose-only by default, everywhere.** `/observe` with `apply:false`, the GUI `observe-run` with `field-apply` unchecked, and `galaxy.py observe` without `--apply` all **write nothing** and call no git/ansible — proven by `test_observe_propose_writes_nothing_and_returns_token` and the e2e dry-run test.
2. **Promote is plan-hash-gated.** A stale/absent `plan_token` on `apply:true` is a **409**, nothing committed (`test_observe_promote_requires_matching_token_else_409`).
3. **The API runs no plays.** A promote invokes `gitio._run` only for `git add/commit/push` — never `ansible-playbook`/`docker` (`test_observe_does_not_run_ansible_or_docker`). The enact commands are **returned**, not executed.
4. **Fail-closed + audited + no creds.** No token ⇒ 503; un-auditable ⇒ 503; the audit line carries method/kind/dashboards/secret-domain-name/plan_token and **never a secret** (`test_observe_audit_excludes_any_secret`).
5. **The picker is filtered.** `/api/telemetry-suggest` returns only applicable methods; a Linux class never offers `snmp`, a switch never offers `host_node` (`test_applicable_methods_filters_by_facts`); `maybe` is never a wrong `no`.
6. **The tripwire passes.** `test_every_privileged_route_is_classified` asserts `/observe` in the privileged set and `/telemetry/*` as inquiry.
7. **Idempotent data write + byte-identical targets.** Re-promote reports `changed:False`; the target bodies equal `gen-observability.py`'s own render, so `gen-observability.py --check` (`validate.sh:72`) stays green after a promote.
8. **`bash tests/validate.sh` green** and `pytest -m "not e2e and not slow"` green, with every new test docstring'd (`check-test-docs.py`), every new testid in `tests/testid_reference.md`, and the doc-sync rows landed in the **same commit**.
9. **Boundary documented, not hidden.** `SECURITY.md` C9 + `docs/api-architecture.md` state that observe is a data-write proposal and agent/exporter bring-up stays `deploy-stack`/Semaphore — the design never claims the network surface enacts infra.

---

**Relevant absolute paths for the implementer.** New: `scripts/kontroll/service/promote.py`, `scripts/kontroll/service/observe.py`, `scripts/kontroll/service/telemetry.py`, `api/routes/observe.py`, `api/routes/telemetry.py`, `tests/unit/test_observe_service.py`, `tests/unit/test_promote.py`, `tests/unit/test_telemetry_service.py`, `tests/integration/test_api_observe.py`, `tests/e2e/test_observe_flow.py`. Modified: `gui/app.py`, `gui/templates/index.html`, `scripts/galaxy.py` (`cmd_observe` + subparser), `api/main.py` (include loop), `api/deps.py` (Catalog.telemetry), `api/models.py` (3 models), `api/ratelimit.py:53` (`_PRIVILEGED`), `tests/integration/test_api_ratelimit.py:182` (set), `tests/testid_reference.md`, `tests/README.md`, `SECURITY.md`, `docs/api-architecture.md`, `docs/logging-architecture.md`, `modules/README.md`, `CHANGELOG.md`. **Hard dependency (Layer 1, owned by the modularity dimension):** `telemetry/<method>.yml` registry, `paths.TELEMETRY_DIR`, `catalog.load_telemetry()`, the `gen-observability.py` method dispatcher with `_entries`/`_render` callable for a single class, and each descriptor's `applies_when`/`agent_role`/`secret_domain`/`params`/`dashboards` fields — this GUI/API flow is the consumer, not the builder, of that registry.
