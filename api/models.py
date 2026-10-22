"""Pydantic response models — the typed contract that auto-generates `/openapi.json` + `/docs`.

The shapes mirror the service layer's returns (records / classify result), so a route returns the
service dict and FastAPI validates + serializes it against the model. kontroll's own OpenAPI
ingester (`galaxy.py openapi`) can read the emitted schema — the dogfooding the design calls for.
"""
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel


class Origin(str, Enum):
    """Where search looks: both sources, only installed (local), or only Galaxy."""
    both = "both"
    local = "local"
    galaxy = "galaxy"


class Capability(BaseModel):
    """One capability cell: the three-valued state + optional confidence/evidence/provenance."""
    state: str
    confidence: Optional[str] = None
    evidence: Optional[str] = None
    provenance: Optional[str] = None


class RecordMeta(BaseModel):
    """Top-level record metadata; `note` is present only when an override annotated the record."""
    description: str = ""
    tags: list[str] = []
    certified: bool = False
    note: Optional[str] = None


class Record(BaseModel):
    """A capability record: metadata + a per-vector capabilities block + the suggested backend."""
    collection: str
    version: Optional[str] = None
    origin: str
    depth: str
    meta: RecordMeta
    capabilities: dict[str, Capability]
    suggested_backend: Optional[str] = None
    backend_candidates: list[str] = []
    provenance: str
    suggested_key: Optional[str] = None     # a search card's class key (S24); absent on a collection's record


class ProbeResponse(BaseModel):
    """A deep probe: the record plus the raw module/plugin facts the CLI also surfaces."""
    record: Record
    modules: list[str]
    plugins: dict[str, list[str]]


class UnitKind(str, Enum):
    """The CLOSED set of runnable-unit kinds GET /units enumerates (R1). `standalone` (in-repo playbooks) is the
    design's deferred third kind — repo-global, not per-collection, so it does not ride GET /units/{collection}."""
    collection_playbook = "collection_playbook"
    role = "role"


class Unit(BaseModel):
    """One runnable unit of a collection: a collection-shipped playbook or a role. `id` = `<collection>/<kind>/<name>`
    — the stable child-projection key the later configure/push stages reference."""
    kind: UnitKind
    name: str
    id: str


class UnitsResponse(BaseModel):
    """The runnable units of one installed collection (empty when the collection ships only modules)."""
    collection: str
    units: list[Unit]


class ConfigureRequest(BaseModel):
    """The operator-submitted configure values for an actuation unit's curated knobs (R3). Re-validated
    server-side against the unit descriptor via service/_validate — the widget is convenience, this is the guard."""
    values: dict[str, Any] = {}


class ConfigureResponse(BaseModel):
    """The server-side validation verdict for a unit's configure values: `ok` (every knob valid) + per-knob
    `errors` (knob_key → message). Read-only — validating stages nothing (design 24 §4: a write-free POST)."""
    key: str
    ok: bool
    errors: dict[str, str] = {}


class UnitRef(BaseModel):
    """The runnable identity of an actuation unit (R4 preview): the playbook/role `name`, the `kind`, and the
    `collection` (None for a standalone in-repo unit)."""
    name: Optional[str] = None
    kind: Optional[str] = None
    collection: Optional[str] = None


class UnitTarget(BaseModel):
    """Where an actuation unit runs (R4 preview): the device class, the inventory group (the one dispatch seam),
    and the blast-radius severity the access-chain header + the R5 blast gate key off."""
    device_class: Optional[str] = None
    inventory_group: Optional[str] = None
    blast_radius: Optional[str] = None


class EnactStep(BaseModel):
    """One post-promote hand-off step (R4 preview): a `kind` (`semaphore`|`operator`), the `task`/`cmd`, and the
    `why`. The API runs NONE of these — they are the operator's make-it-live steps (design 22 §3.5)."""
    kind: str
    task: Optional[str] = None
    cmd: Optional[str] = None
    why: str


class PreviewRequest(BaseModel):
    """The Review-stage preview request (R4): the actuation unit `key` + the operator's submitted configure
    `values`. Re-validated server-side; nothing is staged."""
    key: str
    values: dict[str, Any] = {}


class PreviewResponse(BaseModel):
    """The write-free Review-stage preview (R4, design 24 §5): what the unit WOULD run — the rendered would-run
    `play` (role wrapper / FQCN run-spec, with the derived access-chain header), the resolved non-secret `vars`,
    the `check_first` hand-off line, and the post-promote `enact` steps. Stages nothing."""
    key: str
    unit: UnitRef
    target: UnitTarget
    play: str
    vars: dict[str, Any] = {}
    check_first: str
    enact: list[EnactStep] = []


class TelemetryDeclared(BaseModel):
    """A device-class that already declares a metrics method for this collection (the DECLARED side)."""
    key: str
    methods: list[str] = []


class ClassifyResponse(BaseModel):
    """Which execution backend(s) fit: ordered matches + the best one's name + its requirements (`best`: the first
    match whose registry entry declares a role — the one onboard chooses for a new class; None when none does), plus the
    telemetry methods already DECLARED for this collection in module.yml (the detected/declared reconcile)."""
    collection: str
    matches: list[str]
    best: Optional[str] = None
    requires: list[str] = []
    telemetry_declared: list[TelemetryDeclared] = []


class HealthResponse(BaseModel):
    """Liveness payload."""
    status: str
    service: str
