# Telemetry capability vector (detection)

> **DESIGN HISTORY — not a description of the tree.** This is the design record of the telemetry capability vector, written in June 2026. It was built; [telemetry/README.md](../../telemetry/README.md) and [prometheus/README.md](../../prometheus/README.md) describe what exists. Where it disagrees with the code or with `SECURITY.md`, they are right.

> **Part of the [Option-A observability + onboarding flow](../observability-onboarding-flow.md) design (baseline HEAD `63de129`).**
>
> ⚠️ **Binding contracts (the catalog loader name, the flat `telemetry/<method>.yml` schema, the single clean-cutover dispatcher, the `network` job-vs-inventory-group distinction, the `.env` secret-render mechanism) are pinned in the master design's §4.0 SHARED CONTRACTS.** Where this section names a symbol, schema, or sequencing differently, **§4.0 wins** and this section is reconciled to it before implementation. See the master design's §8 “Resolved conflicts” for the per-section reconciliation list.

This section designs the **telemetry capability vector (detection)** — `vectors/telemetry.yml`, the probe-fact signal it reads, how `predicate.py` evaluates it three-valued (yes/no/maybe), the relationship between a **declared** metrics method (`module.yml`) and a **detected** capability, and how detection feeds a **suggestion** into onboarding and surfaces across search / classify / API / CLI / GUI.

> **Sibling (built):** the **logging** capability vector (instance #3) mirrors this exact model for log ingestion — see [logging-capability-vector.md](logging-capability-vector.md) + [../logging-architecture.md §3.5](../logging-architecture.md).

Scope discipline, honoring the three self-review lenses: **this dimension is PURE, READ-ONLY, and crosses NO trust boundary.** It adds one drop-in vector file, one optional fact channel, and read-only surfacing. It deliberately does **NOT** add the `telemetry/<method>.yml` method registry, the `gen-observability.py` dispatcher, proxy-exporter generation, GUI/API actuation, or the OpenAPI service root — those are other dimensions/phases. The modularity lens's blocker (don't overstate what the vector detects) and major (probe carries no host-OS signal) are folded in as hard constraints below: the vector is honestly scoped as a **collection-signal suggester**, never a host-capability detector.

---

### 0. The one decision that drives everything: what the vector can and cannot see

`predicate.eval_vector` (predicate.py:59) folds a vector's `match` rules against **`facts`**, and `facts` is exactly what `probe._facts()` (probe.py:16) produces:

```python
{"collection", "version", "origin", "depth",
 "description", "tags", "modules", "plugins", "module_options", "certified"}
```

This is **a collection's shape**, not a host's. There is **no** "node_exporter port 9100 is open", **no** "answers SNMP", **no** "has a live /metrics endpoint" — those are *runtime probes of a host*, and `facts` never holds them. Therefore:

| The task asks "detect…" | Reality against the real code | This design's answer |
|---|---|---|
| has a node_exporter port open | not in `facts`; would need a live TCP probe | **NOT detected.** `host_node` (Linux host agent) is **operator-declared**, never vector-detected — the host-OS fact does not exist. The vector reads `telemetry=maybe` for collection-API shapes and `no` otherwise. |
| answers SNMP | not in `facts` | **Suggested, not detected.** A `cliconf`/`netconf` collection → "a proxy-exporter (snmp) *may* fit" → `telemetry=maybe`. |
| exposes a `/metrics` endpoint | not in `facts` for a Galaxy collection; only an OpenAPI spec self-advertises it | **Out of scope here.** That signal belongs to the OpenAPI service root (`openapi.classify_endpoints`), a different dimension. The vector handles the *collection* root only. |
| has an OpenAPI spec with a metrics path | service-root concern | out of scope (deferred). |

So the vector's **honest claim** (stamped in its own header comment and in `docs/capability-matrix.md`) is:

> **`telemetry` = "this collection looks like something a proxy-exporter could scrape via its API/CLI."** It is a *suggester* for the **agent-less / proxy-exporter** methods, computed from collection signals. It does **not** and **cannot** detect that a managed host can run a host-agent (node_exporter) — that is an operator assertion, not a probe fact. Most of the time the cell is `maybe` until a method is declared.

This is the modularity-lens blocker resolved in design: we ship the vector, but we do not let it pretend to be a metrics-exposure detector.

---

### 1. New file: `vectors/telemetry.yml`

**Path:** `vectors/telemetry.yml` (NEW)

It mirrors `vectors/actuate.yml` / `backup.yml` / `bespoke.yml` exactly: `name / label / symbol / order / match[{rule, confidence}]`, loaded by `catalog.load_vectors()` (catalog.py:27, globs `vectors/*.yml`, sorts by `order`) and folded by `predicate.eval_vector` with **zero engine change** because every rule below uses an existing `eval_pred` kind (`plugin` / `module_suffix` / `any_of` / `none_of` — predicate.py:26-41).

```yaml
# Capability vector: telemetry (can this be scraped for metrics?).
#
# SCOPE — read this before adding a rule. A vector matches against a COLLECTION's
# probe facts (modules/plugins/module_options), NOT a live host. So `telemetry` can
# only ever SUGGEST a *proxy-exporter* fit from collection signals (an httpapi/cliconf
# collection is the kind of thing an SNMP/API exporter scrapes). It CANNOT detect that
# a managed host runs a host-agent (node_exporter on :9100) — that is OPERATOR-DECLARED
# in modules/<key>/module.yml (metrics: [{method: host_node}]), never probe-detected,
# because the host's OS is not a collection fact. Treat a `yes` here as "a proxy/API
# exporter is plausible", a `maybe` as "unconfirmable at this depth", a `no` as
# "no scrape-shaped signal". The DECLARED method (module.yml) is the source of truth;
# this vector is the hint the onboarding flow offers. See vectors/actuate.yml for the
# drop-in contract and docs/capability-matrix.md §2 (telemetry is the named 4th vector).
name: telemetry
label: telemetry (scrape)
symbol: telem
order: 4
match:
  # An httpapi/REST collection is the canonical proxy-exporter shape: a control-node
  # exporter (pve-exporter, exportarr, snmp via API) queries it and exposes /metrics.
  # This is the strongest collection-visible signal that "something can scrape this".
  - rule: {plugin: httpapi}
    confidence: medium
  # A network_cli/SNMP-capable device (cliconf/netconf) is an agent-less candidate:
  # snmp_exporter / a CLI-scrape exporter can pull metrics. Suggestive, not certain —
  # the operator still picks the concrete method (snmp, blackbox) at onboarding.
  - rule: {any_of: [{plugin: cliconf}, {plugin: netconf}]}
    confidence: low
  # A *_facts module means structured read-out exists (a textfile/exporter could ship
  # those facts) — a weak hint, lowest confidence.
  - rule: {module_suffix: _facts}
    confidence: low
```

**Field semantics** (identical contract to the other three vectors):

| field | meaning |
|---|---|
| `name: telemetry` | the capabilities-cell key. `record.build_record` emits `rec["capabilities"]["telemetry"]` for **every** record automatically (record.py:20) — no record code changes. |
| `label: telemetry (scrape)` | human label (used in docs/UI tooltips; the GUI maps `symbol`). |
| `symbol: telem` | the short token `cap_cells` prints in the CLI (galaxy.py:72 → `telem:?`). |
| `order: 4` | sort key (catalog.py:33). Slots after `bespoke` (order 3), so it renders last — additive, never reordering the existing three. |
| `match` | ordered rules; `eval_vector` returns the **first rule that evals True** at its `confidence`; else `maybe` if any rule was `None`; else `no` (predicate.py:61-68). |

**Confidence rationale (deliberately conservative):** `httpapi` is `medium` not `high` because "has a REST plugin" ≠ "exposes Prometheus metrics" — it means "a proxy-exporter *could* be built/found for it." `cliconf`/`netconf` is `low` because SNMP/CLI-scrape is plausible but unconfirmed. We never emit `high` from this vector, because no collection signal is *strong* evidence of scrapability. This keeps the vector from drowning the UI in confident-but-wrong `yes` cells (the modularity lens's "mostly noise" warning).

---

### 2. Three-valued behavior — exactly consistent with the existing engine (no code change)

`eval_vector` is unchanged. Walk the three outcomes against `eval_pred` (predicate.py:26-41):

- **`yes`** — first matching rule. e.g. `fortinet.fortios` (has `httpapi`): rule 1 `{plugin: httpapi}` → `bool(facts["plugins"].get("httpapi"))` → `True` → `{state: "yes", confidence: "medium", evidence: "httpapi:fortios"}` (evidence from `pred_evidence`, predicate.py:47-48).
- **`maybe`** — only the **shallow ⊑ deep** soundness path can produce this here, and only if a rule is `None`. **Important:** all three rules of `telemetry` use `plugin` / `module_suffix` / `any_of(plugin…)`, and `eval_pred` for `plugin` returns `bool(...)` (**never `None`** — predicate.py:27) and `module_suffix` returns a `bool` (predicate.py:29). So **`telemetry` never produces `maybe` on its own** — every signal it reads is visible at *both* shallow and deep depth (plugins/modules are in `shallow_from_local` and `shallow_from_galaxy` alike, probe.py:29-35,71-75). This is a **feature, not a gap**: it guarantees `telemetry` is **parity-stable** (`service_search_parity` will class every telemetry cell as `agree`, never `deferred`, never `unsound` — search.py:106-116), so adding this vector cannot break the `unsound == 0` invariant the parity check enforces.
- **`no`** — no rule matched and none was `None`. e.g. a raw-SSH appliance (`openwrt`-style, empty `plugins`, no `_facts`) → all rules `False` → `{state: "no", confidence: None, evidence: None}`.

**Why this matters for the design's correctness contract:** because `telemetry` is built only from depth-stable signals, it is the *safest possible* fourth vector. The parity guarantee (search.py docstring, "a cell only ever softens yes/no to 'maybe', never flips") holds trivially. If a future maintainer adds a `module_option`-based rule (the only `None`-producing kind, predicate.py:31), they introduce a `maybe` path — and they must add the matching parity test (see §7).

---

### 3. The DECLARED ↔ DETECTED relationship (the heart of this dimension)

There are two independent truths about telemetry, and the design must keep them **separate but reconciled**:

| | DECLARED | DETECTED |
|---|---|---|
| **What** | `modules/<key>/module.yml` `metrics:` block (today `{job, via, port}`; under the telemetry-method redesign `{method, params}`). | `rec["capabilities"]["telemetry"]` from `eval_vector`. |
| **Source of truth?** | **YES.** This is what `gen-observability.py` actually generates scrape targets from (gen-observability.py:78). | **NO.** A *hint*. Never auto-writes anything. |
| **Lives where** | the repo, per device-class, operator/onboard-authored. | computed at search/probe time from collection facts. |
| **Scope** | host-agent **and** proxy-exporter (the operator can declare `host_node` for a Linux box even though no fact proves it). | proxy-exporter **suggestion only** (collection signals). |
| **Read by** | `gen-observability.py`, `fetch-dashboards.py`. | search/classify/probe records, the onboarding suggestion. |

**The reconciliation rule (new, additive, read-only):** a record gains an optional `telemetry_declared` flag that says *"this device-class already declares a metrics method in module.yml."* This closes the loop between "I detect telemetry *might* fit" and "the operator already wired it." It is computed by a new pure helper and surfaced in the record's `meta`, so the GUI/CLI can show a "✓ already monitored" vs "scrapable — not yet declared" distinction.

**New pure function** in `scripts/kontroll/service/classify.py` (the natural home — it already owns "what fits this collection"):

```python
# scripts/kontroll/service/classify.py  (MODIFIED — add below service_classify)

import os
import yaml
from kontroll import paths  # add to existing imports

def declared_metrics_methods(collection, fleet=None):
    """Which enabled device-class module(s) declare a `metrics:` block whose collection
    is `collection` — the DECLARED side of telemetry, read from modules/<key>/module.yml.
    Returns a sorted list of {key, methods:[<job-or-method names>]} (possibly empty).
    PURE read of the repo (paths.ROOT-bound, so tmp_repo can repoint it); no probe, no I/O
    beyond reading module.yml + fleet.yml. This is the bridge between a DETECTED telemetry
    capability (the vector) and what the operator has ALREADY wired in module.yml."""
    out = []
    if fleet is None:
        fp = os.path.join(paths.ROOT, "config", "fleet.yml")
        fleet = yaml.safe_load(open(fp, encoding="utf-8")) if os.path.exists(fp) else {}
    for key in (fleet or {}).get("enabled_modules") or []:
        mp = os.path.join(paths.ROOT, "modules", key, "module.yml")
        if not os.path.exists(mp):
            continue
        mod = yaml.safe_load(open(mp, encoding="utf-8")) or {}
        colls = {c.get("name") for c in (mod.get("collections") or [])}
        if collection in colls and mod.get("metrics"):
            # tolerate BOTH schemas: legacy {job, via, port} and new {method, params}.
            methods = [m.get("method") or m.get("job") or "?"
                       for m in (mod.get("metrics") or [])]
            out.append({"key": key, "methods": sorted(methods)})
    return sorted(out, key=lambda d: d["key"])
```

This is **schema-agnostic** (reads `method` if present, falls back to `job`), so it works *today* against the live `{job, via, port}` blocks **and** after the telemetry-method redesign migrates them to `{method, params}` — no coupling to that other dimension's timeline.

---

### 4. How detection feeds a SUGGESTION into onboarding

`build_onboard_plan` (onboard.py:24) is **PURE** and its module dict (onboard.py:46-54) has **no** `metrics:` key today. This dimension does **NOT** make onboarding write a metrics block or actuate anything (that is the GUI-actuation dimension, deferred behind propose-then-promote per the security lens). Instead it adds a **read-only suggestion** to the plan so a human/UI can decide.

**Modify `build_onboard_plan`** to compute and attach a non-mutating suggestion, derived from the same `facts` it already deep-probes (onboard.py:32) plus the vector set:

```python
# scripts/kontroll/service/onboard.py  (MODIFIED)
# add to imports: from kontroll.service.classify import suggest_telemetry

def build_onboard_plan(collection, key, group, host, host_name=None, secrets="network",
                       backend=None, username=None, password=None, api_token=None,
                       backends=None, vectors=None):          # + vectors param
    backends = backends if backends is not None else catalog.load_backends()
    vectors = vectors if vectors is not None else catalog.load_vectors()   # + load vectors
    f = probe.deep_probe(collection)
    if not f["modules"] and not f["plugins"]:
        return {"error": "not_installed"}
    # ... existing backend/role/module construction unchanged ...

    # READ-ONLY telemetry suggestion (no metrics: written; the GUI/operator decides).
    plan_out["telemetry"] = suggest_telemetry(f, vectors)   # added to the returned dict
    return plan_out
```

**New pure function** `suggest_telemetry` in `classify.py` (sits beside `declared_metrics_methods`):

```python
# scripts/kontroll/service/classify.py  (MODIFIED — add)

from kontroll import predicate  # already imported

# Suggestion map: a DETECTED telemetry shape -> the method(s) an operator would pick.
# DATA, not logic — extend by editing this dict (it is the suggestion layer, distinct
# from the telemetry/<method>.yml GENERATION registry that other dimension owns).
_TELEMETRY_HINT = {
    "httpapi": ["proxy-exporter (api)", "host_node"],   # REST device/service: an API exporter; host if it's a box
    "cliconf": ["snmp", "host_node"],                   # network_cli device: snmp_exporter is the agent-less path
    "netconf": ["snmp"],
}

def suggest_telemetry(facts, vectors):
    """The DETECTED telemetry suggestion for a collection's facts: the telemetry vector
    cell + a non-binding list of candidate method names an operator could declare. PURE.
    Returns {cell:{state,confidence,evidence}, candidates:[str], note:str}. `candidates`
    is empty when the cell is 'no'. This is a HINT — it writes nothing; the operator picks
    a method (and that method's GENERATION lives in the telemetry-method registry, not here).
    host_node is always a *possible* manual pick (a host might run node_exporter) but is
    NEVER auto-suggested as primary, because no collection fact proves the host runs Linux."""
    vec = next((v for v in vectors if v["name"] == "telemetry"), None)
    cell = predicate.eval_vector(vec, facts) if vec else {"state": "no", "confidence": None, "evidence": None}
    candidates = []
    if cell["state"] in ("yes", "maybe"):
        for plug in ("httpapi", "cliconf", "netconf"):
            if facts["plugins"].get(plug):
                for m in _TELEMETRY_HINT[plug]:
                    if m not in candidates:
                        candidates.append(m)
    note = ("scrapable — pick a telemetry method to monitor it" if candidates
            else "no scrape-shaped signal; host-agent (host_node) is the only option if it's a host")
    return {"cell": cell, "candidates": candidates, "note": note}
```

**Key design properties** (each ties back to a self-review concern):
- **Writes nothing.** `build_onboard_plan` stays PURE; `apply_onboard_plan` is untouched. No actuation, no new trust boundary (security lens blocker #1/#2 honored — propose-only).
- **`host_node` is never auto-primary.** It appears only as a *manual* candidate, because no fact proves the host runs an agent (modularity lens major #1 honored).
- **The candidate strings are advisory labels**, not method-registry keys this dimension owns. They are data in one dict, trivially extended. The actual generation of an exporter from a chosen method is the other dimension's job; this dimension only *suggests*.

---

### 5. How it surfaces in search / classify / API / CLI / GUI (read-only, mostly free)

Because `record.build_record` emits one cell per vector (record.py:20), the `telemetry` cell appears **everywhere a record appears with zero code change** in the record/search path. The surfacing work is additive presentation.

#### 5.1 CLI — `galaxy.py`
`cap_cells` (galaxy.py:66-73) already iterates **all** vectors and prints `v["symbol"]:MARK`. With `symbol: telem`, every `search` / `probe` row gains a `telem:✓/✗/?` cell **automatically**. The legend at galaxy.py:51 ("Badges: A actuate · B backup · L bespoke") in the GUI hint and the CLI header (galaxy.py:100) are prose — **no count to keep honest**, but the GUI hint string should be extended (below). No change to `cmd_search`/`render_record` needed; verify by running `galaxy.py search fortios --json` and confirming `capabilities.telemetry` is present.

#### 5.2 API — `api/models.py` + `api/routes/classify.py`
- **`api/models.py`:** `Record.capabilities` is already `dict[str, Capability]` (models.py:43) — the `telemetry` cell flows through the typed `/openapi.json` contract with **no model change**. The only optional addition is surfacing the **declared/suggestion** on the classify route:

```python
# api/models.py  (MODIFIED — extend ClassifyResponse, all new fields optional/defaulted
#                  so the contract stays backward-compatible)
class TelemetryDeclared(BaseModel):
    """A device-class that already declares a metrics method for this collection."""
    key: str
    methods: list[str] = []

class ClassifyResponse(BaseModel):
    collection: str
    matches: list[str]
    best: Optional[str] = None
    requires: list[str] = []
    telemetry_declared: list[TelemetryDeclared] = []   # NEW — the DECLARED side (module.yml)
```

- **`api/routes/classify.py`** (and `service_classify`) attach the declared list:

```python
# scripts/kontroll/service/classify.py  (MODIFIED — service_classify returns the declared side)
def service_classify(collection, backends=None):
    # ... existing probe + classify ...
    return {"collection": collection, "matches": matches,
            "best": best["name"] if best else None,
            "requires": (best.get("requires") or []) if best else [],
            "telemetry_declared": declared_metrics_methods(collection)}   # NEW
```
The route (classify.py:12-18) returns the service dict unchanged — FastAPI validates against the extended model. **`GET /classify/{collection}` is `tags=["inquiry"]`, read-only, no token** (classify.py:8) — no privileged surface added, so `test_api_ratelimit.py`'s privileged-route set is **untouched** (security lens: no new privileged route).

#### 5.3 GUI — `gui/templates/index.html` + `tests/testid_reference.md`
The card's badge loop (index.html:83-89) iterates `rec.capabilities` and stamps a `capability-badge` with `data-cap` + `data-state` for **every** cell — so a `telem` badge **renders automatically**. Two small, doc-synced changes:

1. **`SYM` map** (index.html:59) add `telemetry:'T'`:
   ```js
   const SYM = {actuate:'A', backup:'B', bespoke:'L', telemetry:'T'};
   ```
2. **The legend hint** (index.html:51) extend prose:
   ```html
   <div class="hint">Badges: <b>A</b> actuate · <b>B</b> backup · <b>L</b> bespoke/local
     · <b>T</b> telemetry (scrapable) · → suggested backend. ✓ confirmed, ? unknown at this depth.</div>
   ```

**`data-testid`:** the telemetry badge reuses the **existing** `capability-badge` testid with the new discriminator `data-cap="telemetry"` (the pattern is already documented at testid_reference.md:41). The **only** `tests/testid_reference.md` change is to extend that row's discriminator enumeration:

```
| `capability-badge` | a per-capability badge | `data-cap="actuate\|backup\|bespoke\|telemetry"` + `data-state="yes\|no\|maybe"` |
```

No new interactive element is added in this dimension (no telemetry-method picker — that is the GUI-actuation dimension), so **no new testid row**, only the discriminator update. This keeps the GUI change minimal and within the read-only scope.

---

### 6. Optional fact channel (DEFERRED, designed for completeness)

The task lists "has a /metrics endpoint" / "answers SNMP" as detection targets. These need a fact `probe._facts` does not carry. **This dimension does NOT add them** (they would be a live-host probe, not a collection probe, and would require extending `_facts` + both probers + every `make_facts` call — engine work the modularity lens flags as out-of-scope for a clean refactor). The design **records the seam** so a later phase can add it cleanly:

> **Future fact `exposes_metrics`** (a `bool`/`None` channel on `_facts`, populated only by a *live* prober the service root would own, e.g. `openapi.classify_endpoints` detecting a `/metrics` path → sets `exposes_metrics=True`; absent on Galaxy/local collection probes → `None`). A new `eval_pred` kind `metrics_endpoint` (predicate.py, after the `module_option` branch) would read it, returning `None` when the channel is unpopulated (preserving three-valued honesty). The telemetry vector would then gain a **high-confidence** first rule `{rule: {metrics_endpoint: true}, confidence: high}`. **Until that live prober exists, this rule is omitted** — adding it without a populating prober would make `telemetry` perpetually `maybe`, which is noise. This is the explicit upgrade path, not a TODO in code.

---

### 7. Tests — every one with a docstring (what it verifies + the failure it guards), per `tests/check-test-docs.py`

**NEW file:** `tests/unit/test_telemetry_vector.py`

```python
"""The telemetry capability vector (vectors/telemetry.yml) — the DETECTED side.

These pin that the 4th vector loads, is depth-stable (no module_option rule, so it
never produces a 'maybe' on its own — the parity-safety property), and resolves the
three states from the right collection signals. They guard the vector from silently
never matching, and from being wrongly upgraded to a 'maybe'-producing rule without
the matching parity test.
"""
import pytest
import galaxy
pytestmark = pytest.mark.unit


def test_telemetry_vector_is_loaded_and_ordered_last(vectors):
    """vectors/telemetry.yml is discovered by load_vectors and sorts after the launch
    three (order:4) — guards against the drop-in not being picked up, or reordering the
    existing actuate/backup/bespoke cells (which the CLI/GUI render positionally)."""
    names = [v["name"] for v in vectors]
    assert "telemetry" in names
    assert names.index("telemetry") == len(names) - 1   # order 4 => last


def test_telemetry_yes_from_httpapi(vectors, make_facts):
    """An httpapi (REST) collection yields telemetry=yes at medium confidence — the
    proxy-exporter-shaped signal. Guards the vector silently never matching its primary rule."""
    telem = next(v for v in vectors if v["name"] == "telemetry")
    f = make_facts(plugins={"httpapi": ["fortios"]})
    res = galaxy.eval_vector(telem, f)
    assert res["state"] == "yes" and res["confidence"] == "medium"
    assert res["evidence"].startswith("httpapi")


def test_telemetry_yes_low_from_cliconf(vectors, make_facts):
    """A network_cli (cliconf) device yields telemetry=yes at LOW confidence — the
    agent-less snmp candidate. Guards the confidence ordering (cliconf must not read as
    high like backup does; scrapability is a weaker claim than backup)."""
    telem = next(v for v in vectors if v["name"] == "telemetry")
    f = make_facts(plugins={"cliconf": ["ios"]})
    res = galaxy.eval_vector(telem, f)
    assert res["state"] == "yes" and res["confidence"] == "low"


def test_telemetry_no_for_raw_ssh_appliance(vectors, make_facts):
    """A raw-SSH appliance (no plugins, no _facts module) yields telemetry=no — the
    honest 'no scrape-shaped signal' case. Guards against a false-positive scrapable label
    on a device that genuinely exposes nothing collection-visible."""
    telem = next(v for v in vectors if v["name"] == "telemetry")
    f = make_facts(modules=["thing"], plugins={})
    assert galaxy.eval_vector(telem, f)["state"] == "no"


def test_telemetry_is_depth_stable_never_maybe(vectors, make_facts):
    """telemetry uses only plugin/module_suffix rules (visible at BOTH shallow and deep),
    so it never reads 'maybe' — the parity-safety property. Guards a future maintainer from
    adding a module_option (deep-only) rule without also adding a parity test: if this flips
    to 'maybe' for an all-shallow fact set, the soundness contract changed silently."""
    telem = next(v for v in vectors if v["name"] == "telemetry")
    # shallow-shaped facts (module_options empty, as shallow_from_* leaves them)
    f = make_facts(plugins={"cliconf": ["ios"]}, module_options={})
    assert galaxy.eval_vector(telem, f)["state"] != "maybe"
```

**NEW file:** `tests/unit/test_telemetry_suggest.py`

```python
"""suggest_telemetry + declared_metrics_methods — the DETECTED->suggestion bridge and the
DECLARED reconciliation. Guards that detection produces a non-binding method hint (never a
write), that host_node is never auto-primary (no fact proves the host runs an agent), and
that the declared side reads BOTH the legacy {job,via} and new {method} module.yml schemas.
"""
import os
import pytest
import yaml
from kontroll import paths
from kontroll.service.classify import suggest_telemetry, declared_metrics_methods
pytestmark = pytest.mark.unit


def test_suggest_offers_candidates_for_httpapi(vectors, make_facts):
    """An httpapi collection's suggestion has a non-empty candidate list including an API
    exporter — the onboarding hint. Guards detection producing no actionable suggestion."""
    s = suggest_telemetry(make_facts(plugins={"httpapi": ["fortios"]}), vectors)
    assert s["cell"]["state"] == "yes"
    assert any("api" in c for c in s["candidates"])


def test_suggest_never_makes_host_node_primary(vectors, make_facts):
    """host_node is only ever a trailing/manual candidate, never first — because no collection
    fact proves the managed host runs a node_exporter agent. Guards the design's hard rule that
    host-agent telemetry is OPERATOR-DECLARED, not vector-detected."""
    s = suggest_telemetry(make_facts(plugins={"httpapi": ["x"]}), vectors)
    assert not s["candidates"] or s["candidates"][0] != "host_node"


def test_suggest_empty_candidates_when_no(vectors, make_facts):
    """A no-signal device yields telemetry=no and an empty candidate list with the honest note —
    guards against suggesting a method for something with nothing to scrape."""
    s = suggest_telemetry(make_facts(modules=["thing"]), vectors)
    assert s["cell"]["state"] == "no" and s["candidates"] == []


def test_declared_reads_legacy_and_method_schema(tmp_repo):
    """declared_metrics_methods finds an enabled module that declares a metrics block for the
    collection, under BOTH the legacy {job,via,port} and the new {method,params} schema — the
    DECLARED side that the telemetry-method redesign must not break. Guards the detection/declared
    bridge silently going blind when module.yml migrates to the method schema."""
    (tmp_repo / "modules" / "demo").mkdir(parents=True)
    (tmp_repo / "modules" / "demo" / "module.yml").write_text(yaml.safe_dump({
        "key": "demo", "collections": [{"name": "ns.demo"}],
        "metrics": [{"method": "host_node"}, {"job": "proxmox", "via": "proxy"}],
    }), encoding="utf-8")
    fleet = {"enabled_modules": ["demo"]}
    got = declared_metrics_methods("ns.demo", fleet=fleet)
    assert got and got[0]["key"] == "demo"
    assert set(got[0]["methods"]) == {"host_node", "proxmox"}   # method OR job, both read


def test_declared_empty_for_unmonitored_collection(tmp_repo):
    """A collection no enabled module declares metrics for returns [] — guards a false 'already
    monitored' claim (e.g. cisco_ios today, which has no metrics: block)."""
    (tmp_repo / "modules" / "demo").mkdir(parents=True)
    (tmp_repo / "modules" / "demo" / "module.yml").write_text(yaml.safe_dump({
        "key": "demo", "collections": [{"name": "ns.demo"}]}), encoding="utf-8")
    assert declared_metrics_methods("ns.demo", fleet={"enabled_modules": ["demo"]}) == []
```

**MODIFY** `tests/unit/test_build_record.py::test_record_schema_and_backend_suggestion` — its assertion `set(rec["capabilities"]) == {v["name"] for v in vectors}` (test_build_record.py:27) is **vector-set-agnostic** and stays green automatically (it compares against the loaded vectors, so adding `telemetry` is absorbed). **No edit needed**, but add one targeted assertion in the same commit to pin the new cell's presence:

```python
    assert "telemetry" in rec["capabilities"]   # the 4th vector cell is emitted (added with vectors/telemetry.yml)
```

**MODIFY** `tests/integration/test_api_classify.py` (or wherever the classify route is integration-tested) — add a test that `GET /classify/<coll>` returns `telemetry_declared` (defaulted `[]` for an undeclared collection), docstring: *"the classify response surfaces the DECLARED telemetry methods; an undeclared collection returns an empty list, not a 500 — guards the new optional field breaking the typed contract."*

**No `test_predicate_engine.py` change** — `telemetry` reuses existing kinds, so the truth tables there already cover its evaluation (the `plugin`/`module_suffix`/`any_of` rows). If §6's `metrics_endpoint` kind is ever added, *that* adds a truth-table row.

---

### 8. Logging + audit (`kontroll_run_id`-correlated; action, never the credential)

This dimension is **read-only** — search/classify/probe are `tags=["inquiry"]`, **not** privileged, so they do **not** call `audit_action` (that is for mutations, api/auth.py:54). Therefore **no new audit line and no credential is ever in scope here.** The only logging is the existing `logging.getLogger("kontroll.service.*")` debug/warn surface. Two read-only log lines are added for diagnosability (no secret, no run_id needed because no actuation):

```python
# scripts/kontroll/service/classify.py
log = logging.getLogger("kontroll.service.classify")
log.debug("telemetry suggest %s -> state=%s candidates=%s",
          facts["collection"], cell["state"], candidates)        # no credential, no host secret
log.debug("telemetry declared for %s: %s", collection, [d["key"] for d in out])
```

**When this DETECTED suggestion later becomes a GUI-actuated `metrics:` write** (the deferred GUI-actuation dimension), THAT dimension owns the audit line — and it must read, per the security lens: `audit_action(request, principal, "observability-enable", "key=%s method=%s" % (key, method))` — the **method name only, never any exporter secret (SNMP community / API key)**, correlated by the `principal.run_id` minted in `require_token` (api/auth.py:40) and threaded to Ansible as `-e kontroll_run_id=<id>`. This dimension stops at the *suggestion*; it explicitly does not emit that line.

---

### 9. SECURITY.md — no control change, one accepted-fact note

This dimension adds **no secret, no trust boundary, no privileged surface**: it reads collection facts and `module.yml` (already-committed, secret-free) and surfaces a read-only label. **No `Cn` control is added or amended.** The one honesty note to add under the existing observability control (C9), so the threat model records the scoping decision:

> **C9 note (telemetry detection, read-only):** the `telemetry` capability vector and the onboarding `suggest_telemetry` hint are **derivation, not actuation** — they read collection facts + committed `module.yml` and emit a label/suggestion. They write nothing, touch no secret, and add no privileged route. The exporter *secrets* and the actuation that *acts on* a chosen method are governed where that method is generated/provisioned (the telemetry-method registry + the GUI-actuation route), each behind its own C-control. **Proves:** `tests/unit/test_telemetry_suggest.py::test_suggest_offers_candidates_for_httpapi` confirms the suggestion is non-binding (a candidate list, not a write); the route is `tags=["inquiry"]` (unauthenticated, read-only), so `test_api_ratelimit.py::test_every_privileged_route_is_classified` is unaffected (no new privileged path).

---

### 10. Documentation-Sync rows (same commit)

| Changed | Update |
|---|---|
| `vectors/telemetry.yml` (NEW 4th vector) | `docs/capability-matrix.md` §2 — telemetry is *named* there as the example 4th vector (lines 34-38); move it from "adding a 4th later (e.g. `telemetry`)" to a **launch-set table row** with its signal column (`httpapi`/`cliconf`/`_facts`) and the explicit "suggester, not host-detector" caveat. |
| `vectors/telemetry.yml` | `vectors/` has no README; the drop-in contract is documented in `vectors/actuate.yml`'s header comment — telemetry's own header (above) carries the scope caveat, satisfying the convention. |
| `Record.capabilities` gains a `telemetry` cell (no model change) + `ClassifyResponse.telemetry_declared` (NEW field) | `api/models.py` is self-documenting via `/openapi.json`; add a one-line note in `docs/api-architecture.md` that classify now surfaces the declared-telemetry reconciliation. |
| GUI badge gains `data-cap="telemetry"` | `tests/testid_reference.md` — extend the `capability-badge` discriminator row (§5.3); grep-verify `data-cap="…telemetry…"`. |
| New tests + the suggestion/declared seam | `tests/README.md` — add `test_telemetry_vector.py` and `test_telemetry_suggest.py` to the unit inventory; note `classify.declared_metrics_methods`/`suggest_telemetry` as PURE (no new mock seam — they read the real tree via `paths.ROOT`, covered by `tmp_repo`). |
| Behaviour-affecting | `CHANGELOG.md` `[Unreleased]` — "feat(onboarding): telemetry capability vector — detected scrape-suitability surfaced in search/classify + a read-only telemetry suggestion in the onboard plan (declared metrics methods reconciled from module.yml); no actuation." |
| `module.yml` `metrics:` is now also *read* by the detection bridge | `modules/README.md` — note that `metrics:` is the DECLARED source of truth that `classify.declared_metrics_methods` reconciles against the DETECTED telemetry vector (one sentence; no schema change to `metrics:` in this dimension). |

---

### 11. Acceptance criteria

1. `vectors/telemetry.yml` exists; `catalog.load_vectors()` returns it last (`order: 4`); every `build_record` output gains a `capabilities.telemetry` cell (proved by `test_build_record.py` assertion + `test_telemetry_vector.py`).
2. `eval_vector(telemetry, …)` returns `yes`(httpapi=medium / cliconf=low), `no`(raw-ssh), and **never `maybe`** for shallow-shaped facts — the parity-safety property (`test_telemetry_is_depth_stable_never_maybe`).
3. `service_search_parity` over any keyword set still reports **`unsound == 0`** and classes every `telemetry` cell as `agree` (never `deferred`/`unsound`) — adding the vector does not perturb the soundness invariant. (Verify by running `galaxy.py verify-parity` live or asserting in an integration test that `summary["unsound"] == 0`.)
4. `suggest_telemetry` returns a non-empty candidate list for an httpapi/cliconf collection, an empty list + honest note for a no-signal device, and **never lists `host_node` first** (`test_telemetry_suggest.py`).
5. `declared_metrics_methods` reads BOTH the legacy `{job,via,port}` and new `{method,params}` module.yml schemas and returns `[]` for an undeclared collection (proved with `tmp_repo`).
6. `GET /classify/{collection}` returns `telemetry_declared` (defaulted `[]`), validates against `ClassifyResponse`, stays `tags=["inquiry"]` — `test_api_ratelimit.py`'s privileged set is **unchanged**.
7. CLI `galaxy.py search … --json` shows `capabilities.telemetry`; the human render shows a `telem:✓/✗/?` cell (free via `cap_cells`).
8. GUI renders a `capability-badge[data-cap="telemetry"]` per card with the `T` symbol; `tests/testid_reference.md` discriminator updated and grep-verified.
9. `build_onboard_plan` returns `plan["telemetry"]` and **writes nothing** different — `apply_onboard_plan` is byte-for-byte unchanged in behaviour; no actuation, no new audit line, no secret touched.
10. `bash tests/validate.sh` green (`check-test-docs.py` passes — every new test has its docstring; `pytest -m "not e2e and not slow"` green). No new `--check` generator guard is required because this dimension generates **no** files.
11. **Scope guard:** no `telemetry/<method>.yml`, no `gen-observability.py` edit, no `prometheus.yml` edit, no privileged route, no GUI actuation control — confirmed by `git diff --stat` touching only `vectors/telemetry.yml`, `scripts/kontroll/service/classify.py`, `scripts/kontroll/service/onboard.py`, `api/models.py`, `gui/templates/index.html`, the test files, and the doc-sync targets.

**Files touched (complete list):**
- NEW `vectors/telemetry.yml`
- NEW `tests/unit/test_telemetry_vector.py`, `tests/unit/test_telemetry_suggest.py`
- MOD `scripts/kontroll/service/classify.py` (`declared_metrics_methods`, `suggest_telemetry`, `service_classify` return), `scripts/kontroll/service/onboard.py` (plan `telemetry` key, `vectors` param), `api/models.py` (`TelemetryDeclared`, `ClassifyResponse.telemetry_declared`), `gui/templates/index.html` (`SYM`, legend hint), `tests/unit/test_build_record.py` (one assertion), the classify integration test
- DOC `docs/capability-matrix.md`, `tests/testid_reference.md`, `tests/README.md`, `CHANGELOG.md`, `docs/api-architecture.md`, `modules/README.md`, `SECURITY.md` (C9 note)
