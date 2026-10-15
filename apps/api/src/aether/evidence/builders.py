"""Evidence and Measurement construction with C7 validation (before anything reaches the DB).

Numbers exist only as Measurements; ``facts`` hold non-numeric attributes only. A ratio
(statistic ``fraction``) must name its denominator, which must be a Measurement in the same
evidence. IDs are assigned here so the content hash covers them deterministically.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass, field
from typing import Any

import shapely
from shapely.geometry import shape

from aether import science
from aether.domain import registries
from aether.domain.errors import InvariantViolation

# Facts keys whose values may legitimately be numeric identifiers (C7 §9 allow-list).
NUMERIC_FACT_KEYS = frozenset({"epsg", "utm_zone", "utm_epsg"})


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass
class MeasurementDraft:
    quantity: str
    statistic: str
    value: float | int | None
    method: str
    region: str = "aoi"
    unavailable_reason: str | None = None
    denominator: str | None = None  # quantity name of the denominator measurement in this evidence
    n_samples: int | None = None
    measurement_id: str = field(default_factory=lambda: new_id("ms"))

    @property
    def status(self) -> str:
        return "unavailable" if self.value is None else "available"


@dataclass
class EvidenceDraft:
    kind: str
    facts: dict[str, Any]
    measurements: list[MeasurementDraft]
    limitations: list[str] = field(default_factory=list)
    geometry: dict[str, Any] | None = None  # GeoJSON, EPSG:4326
    observed_period_a: tuple[str, str] | None = None  # inclusive ISO dates
    artifact_keys: list[str] = field(default_factory=list)  # resolved to artifact UUIDs at persist time
    evidence_id: str = field(default_factory=lambda: new_id("ev"))

    def measurement(self, quantity: str, statistic: str | None = None) -> MeasurementDraft:
        for m in self.measurements:
            if m.quantity == quantity and (statistic is None or m.statistic == statistic):
                return m
        raise KeyError(quantity)


def _check_facts(obj: Any, path: str = "facts") -> None:
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return
    if isinstance(obj, (int, float)):
        key = path.rsplit(".", 1)[-1]
        if key not in NUMERIC_FACT_KEYS:
            raise InvariantViolation("facts_contain_number", f"numeric value in {path}; numbers must be Measurements")
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            _check_facts(v, f"{path}.{k}")
        return
    if isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _check_facts(v, f"{path}[{i}]")
        return
    raise InvariantViolation("facts_type", f"unsupported facts value at {path}: {type(obj).__name__}")


def validate_evidence(ev: EvidenceDraft) -> None:
    _check_facts(ev.facts)
    for code in ev.limitations:
        registries.limitation(code)
    by_quantity = {m.quantity: m for m in ev.measurements}
    for m in ev.measurements:
        entry = registries.resolve_quantity(m.quantity)
        if m.statistic not in entry.statistics:
            raise InvariantViolation("statistic_not_allowed", f"{m.statistic} not allowed for {m.quantity}")
        if m.value is not None:
            if not isinstance(m.value, (int, float)) or isinstance(m.value, bool) or not math.isfinite(m.value):
                raise InvariantViolation("measurement_not_finite", f"{m.quantity} value {m.value!r}")
            if m.unavailable_reason is not None:
                raise InvariantViolation("measurement_status", f"{m.quantity} has a value and a reason")
        elif m.unavailable_reason not in registries.UNAVAILABLE_REASONS:
            raise InvariantViolation("unavailable_without_reason", f"{m.quantity} unavailable without valid reason")
        expected = registries.denominator_for(m.quantity)
        if m.statistic == "fraction":
            if expected is None or m.denominator != expected:
                raise InvariantViolation("fraction_denominator", f"{m.quantity} must use denominator {expected}")
            if expected not in by_quantity:
                raise InvariantViolation("fraction_denominator", f"denominator {expected} missing from evidence")
    if ev.geometry is not None and not shape(ev.geometry).is_valid:
        raise InvariantViolation("evidence_geometry_invalid", "evidence geometry is invalid")


def unit_of(quantity: str) -> str:
    return registries.resolve_quantity(quantity).unit


def content_sha256(ev: EvidenceDraft, processing_run_id: str) -> str:
    """C7 §5: sha256 over canonical JSON of kind, facts, geometry WKB hex, measurements, limitations, run."""
    geom_hex = shapely.to_wkb(shape(ev.geometry), hex=True, output_dimension=2) if ev.geometry else None
    payload = {
        "kind": ev.kind,
        "facts": ev.facts,
        "geometry_wkb_hex": geom_hex,
        "measurements": [
            {
                "id": m.measurement_id,
                "quantity": m.quantity,
                "statistic": m.statistic,
                "value": m.value,
                "status": m.status,
                "unavailable_reason": m.unavailable_reason,
            }
            for m in sorted(ev.measurements, key=lambda m: m.measurement_id)
        ],
        "limitations": [registries.limitation(c) for c in ev.limitations],
        "processing_run_id": processing_run_id,
        "schema_version": science.EVIDENCE_SCHEMA_VERSION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def canonical_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
