"""API request/response models (OpenAPI → generated TypeScript types in apps/web)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


class Project(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    created_at: datetime


class AoiIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    geometry: dict[str, Any] = Field(description="GeoJSON Polygon or MultiPolygon, EPSG:4326 lon/lat (RFC 7946)")
    source: Literal["drawn", "geojson"] = "drawn"


class Aoi(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID | None = None
    name: str
    geometry: dict[str, Any]
    geometry_hash: str
    utm_epsg: int
    normalization: list[str] | None = None
    evidence_id: str | None = Field(default=None, description="aoi_measurement evidence (geodesic area)")
    created_at: datetime


class PeriodIn(BaseModel):
    start: date
    end: date


class CatalogSearchIn(BaseModel):
    aoi_id: uuid.UUID
    period: PeriodIn
    max_tile_cloud: float = Field(default=80.0, gt=0, le=100, description="Tile-level pre-filter (not AOI cloud)")


class Measurement(BaseModel):
    measurement_id: str
    quantity: str
    statistic: str
    status: Literal["available", "unavailable"]
    value: float | None
    unavailable_reason: str | None
    unit: str
    denominator_quantity: str | None = None
    denominator_measurement_id: str | None = None
    n_samples: int | None = None
    region: str | None = None
    geometry_evidence_id: str | None = None
    method: str | None = None


class SceneMetadataRef(BaseModel):
    evidence_id: str
    measurements: list[Measurement]


class SceneRow(BaseModel):
    item_id: str
    acquired_at: str | None
    platform: str
    processing_baseline: str | None
    datatake_id: str | None
    grid_code: str | None
    status: Literal["used", "excluded"]
    reason_code: str | None
    metadata_evidence: SceneMetadataRef | None = None


class CatalogSearchOut(BaseModel):
    scene_search_id: uuid.UUID
    evidence_id: str
    number_matched: int | None
    observations_eligible: int
    n_min: int
    scenes: list[SceneRow]


class AnalysisIn(BaseModel):
    """No scientific parameters, collection or threshold fields exist (C5 §9, C-09).

    Unknown fields are kept (not validated) so snapshot requests behave exactly as in Phase 1, where they
    were ignored (plan §18); change requests reject them (no override can be smuggled in, C-09).
    """

    model_config = ConfigDict(extra="allow")

    aoi_id: uuid.UUID
    type: Literal["snapshot", "change"] = "snapshot"
    period_a: PeriodIn
    period_b: PeriodIn | None = None


class AnalysisSubmitted(BaseModel):
    analysis_run_id: uuid.UUID
    job_id: uuid.UUID | None
    status: str
    cached: bool


class Job(BaseModel):
    id: uuid.UUID
    kind: str
    analysis_run_id: uuid.UUID | None
    status: str
    stage: str | None
    progress: float
    attempts: int
    max_attempts: int
    error: dict[str, Any] | None
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class EvidenceRef(BaseModel):
    evidence_id: str
    kind: str
    index: str | None
    period: Literal["a", "b", "delta"] | None = None  # change runs: per-period vs delta statistics
    event_label: int | None = None  # change_event evidence (C-17 global label)
    limitations: list[dict[str, Any]]


class RunInput(BaseModel):
    item_id: str
    datatake_id: str | None
    status: str
    reason_code: str | None
    acquired_at: datetime | None
    platform: str
    processing_baseline: str | None


class AnalysisSummary(BaseModel):
    id: uuid.UUID
    aoi_id: uuid.UUID
    type: str
    period_start: date
    period_end: date
    period_b_start: date | None = None  # change runs only (plan §12)
    period_b_end: date | None = None
    status: str
    outcome: str | None
    created_at: datetime
    completed_at: datetime | None


class AnalysisDetail(AnalysisSummary):
    project_id: uuid.UUID
    collection: str
    parameters: dict[str, Any]
    algorithm_versions: dict[str, str]
    grid_hash: str
    job_id: uuid.UUID | None
    job: Job | None = None
    evidence: list[EvidenceRef]
    inputs: list[RunInput]
    calibration: list[dict[str, Any]]


class Limitation(BaseModel):
    code: str
    blocking: bool
    text: str


class Evidence(BaseModel):
    evidence_id: str
    schema_version: str
    project_id: uuid.UUID
    analysis_run_id: uuid.UUID | None
    processing_run_id: uuid.UUID
    kind: str
    geometry: dict[str, Any] | None
    period_start: date | None
    period_end: date | None
    period_b_start: date | None = None  # observed_period_b (C-16)
    period_b_end: date | None = None
    geometry_artifact_id: uuid.UUID | None = None  # authoritative native geometry (change_events.gpkg)
    facts: dict[str, Any]
    confidence_band: str
    limitations: list[Limitation]
    artifact_ids: list[uuid.UUID]
    content_sha256: str
    created_at: datetime
    measurements: list[Measurement]


class Layer(BaseModel):
    artifact_id: uuid.UUID
    name: str
    kind: str
    render: str
    tile_url: str
    legend: dict[str, Any]
    grid: str
    resolution_m: float
    sha256: str
    period: Literal["a", "b", "delta"] | None = None  # change runs; null for snapshots


class CompareOut(BaseModel):
    allowed: bool
    label: str
    period_a: tuple[str, str]
    period_b: tuple[str, str]


# --- change analyses (Phase 2) ----------------------------------------------------------------------


class PreflightPeriod(BaseModel):
    start: date
    end: date
    days: int
    items_considered: int
    observations_eligible: int
    sufficient_observations_possible: bool


class ChangePreflightOut(BaseModel):
    """STAC-level disclosure (plan §12, C-15); joint coverage and registration support are decided in the job."""

    type: Literal["change"]
    pair_rules: Literal["passed"]
    periods: dict[Literal["a", "b"], PreflightPeriod]
    grid: dict[str, dict[str, Any]]
    n_min: int
    sufficient_observations_possible: bool
    coregistration_assessable_by_extent: bool
    coregistration_note: str
    outcome_order: list[str]
    note: str
    scenes: dict[Literal["a", "b"], list[SceneRow]]


Sign = Literal["-", "0", "+"]


class ChangeEventProperties(BaseModel):
    evidence_id: str
    event_label: int
    category: Literal["water_increase", "water_decrease", "built_bare_increase", "veg_loss", "veg_gain", "other_significant"]
    touches_aoi_boundary: bool
    confidence_band: Literal["higher", "moderate", "lower"]
    index_flags: dict[Literal["ndvi", "mndwi", "ndbi"], list[Sign]]
    mode: Literal["robust", "absolute_only"]
    geometry_artifact_id: uuid.UUID
    limitations: list[Limitation]
    measurements: list[Measurement]


class ChangeFeature(BaseModel):
    type: Literal["Feature"]
    id: int
    geometry: dict[str, Any] = Field(description="EPSG:4326 copy; authoritative UTM geometry is the GeoPackage")
    properties: ChangeEventProperties


class ChangeFeatureCollection(BaseModel):
    type: Literal["FeatureCollection"]
    analysis_run_id: uuid.UUID
    outcome: str | None
    feature_count: int
    features: list[ChangeFeature]
