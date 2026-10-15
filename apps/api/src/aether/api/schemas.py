"""API request/response models (OpenAPI → generated TypeScript types in apps/web)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


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


class CompareOut(BaseModel):
    allowed: bool
    label: str
    period_a: tuple[str, str]
    period_b: tuple[str, str]
