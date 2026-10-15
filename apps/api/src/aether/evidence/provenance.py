"""Typed provenance steps (C2 §6, C7 §6) and the processing-run environment allow-list.

Every value-affecting transformation is a step validated against its schema before commit.
``aoi_normalization`` and ``catalog_search`` extend the C7 §6 list for the AOI and search runs.
"""

from __future__ import annotations

import platform
import subprocess
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter

from aether.domain.errors import InvariantViolation


class _Step(BaseModel):
    model_config = ConfigDict(extra="allow")
    # C-16: optional period tag of per-period steps in change runs; absent on Phase 1 records.
    period: Literal["a", "b"] | None = None


class SourceRead(_Step):
    kind: Literal["source_read"]
    item_id: str
    asset_key: str
    href: str
    file_checksum: str | None
    native_epsg: int
    native_transform: list[float]
    window: list[int]
    dtype: str


class RadiometricNormalization(_Step):
    kind: Literal["radiometric_normalization"]
    version: str
    observation_id: str
    item_ids: list[str]
    asset_key: str
    scale: float
    offset: float
    nodata: int
    metadata_source: str
    invalid_px: dict[str, int]


class SourceReadFailed(_Step):
    """Read-time exclusion of an item (C1 §8 / C3 §4 / C2 §3), with transport attempts if any."""

    kind: Literal["source_read_failed"]
    item_id: str
    reason_code: str
    attempts: list[dict[str, str]]


class Masking(_Step):
    kind: Literal["masking"]
    profile: str
    valid_classes: list[int]
    dilated_classes: list[int]
    dilation_radius_px: int
    class_counts: dict[str, int]


class Resampling(_Step):
    kind: Literal["resampling"]
    rule: Literal["R1", "R2", "R3"]
    data_class: str
    method: str
    source_grid: str
    target_grid: str
    purpose: str


class Index(_Step):
    kind: Literal["index"]
    version: str
    index: str
    formula: str
    bands: dict[str, str]
    grid: str
    undefined_rule: str
    counts: dict[str, int]


class Compositing(_Step):
    kind: Literal["compositing"]
    version: str
    index: str
    statistic: Literal["median"]
    strategy: Literal["per_observation_index_then_temporal_median"]
    n_min: int
    observation_ids: list[str]


class DisplayComposite(_Step):
    """Per-band median RGB for map display only (C4 §3); never a measurement input."""

    kind: Literal["display_composite"]
    purpose: Literal["display_rgb"]
    bands: list[str]
    statistic: Literal["median"]
    artifact_sha256: str


class QaCompositeStep(_Step):
    """NIR QA composite (C4 §3): sole co-registration input; never a measurement; never filled."""

    kind: Literal["qa_composite"]
    version: str
    purpose: Literal["coregistration_qa"]
    band: Literal["nir"]
    statistic: Literal["median"]
    n_min: int
    observation_ids: list[str]
    filled: Literal[False]


class Coregistration(_Step):
    """C3 §9 shift check (scalars only; the estimator fill never leaves the estimator, C-01)."""

    kind: Literal["coregistration"]
    version: str
    method: str
    upsample_factor: int
    status: Literal["ok", "failed", "not_assessable"]
    joint_valid_px: int
    grid_px: int


class ChangeRules(_Step):
    kind: Literal["change_rules"]
    version: str
    mode: Literal["robust", "absolute_only"]
    parameters: dict[str, Any]
    category_px: dict[str, int]


class Morphology(_Step):
    kind: Literal["morphology"]
    version: str
    structuring_element: Literal["square_3x3"]
    sequence: list[str]
    connectivity: Literal[8]
    mmu_m2: float
    mmu_min_px: int
    per_category: dict[str, dict[str, int]]


class Polygonization(_Step):
    kind: Literal["polygonization"]
    method: str
    connectivity: Literal[8]
    geometry: Literal["pixel_edges"]
    simplification: None
    validity_rule: str
    events: int
    repair_count: int


class Statistics(_Step):
    kind: Literal["statistics"]
    method: str
    grid: str
    available: bool


class AoiNormalization(_Step):
    kind: Literal["aoi_normalization"]
    operations: list[str]
    geometry_hash: str
    area_method: str


class CatalogSearch(_Step):
    kind: Literal["catalog_search"]
    api_url: str
    collection: str
    request: dict[str, Any]
    item_ids: list[str]


Step = (
    SourceRead
    | SourceReadFailed
    | RadiometricNormalization
    | Masking
    | Resampling
    | Index
    | Compositing
    | DisplayComposite
    | QaCompositeStep
    | Coregistration
    | ChangeRules
    | Morphology
    | Polygonization
    | Statistics
    | AoiNormalization
    | CatalogSearch
)
_ADAPTER: TypeAdapter[Any] = TypeAdapter(Step)


def validate_steps(steps: list[dict[str, Any]]) -> None:
    for i, s in enumerate(steps):
        try:
            _ADAPTER.validate_python(s)
        except Exception as exc:
            raise InvariantViolation("provenance_step_invalid", f"step {i} ({s.get('kind')}): {exc}") from exc


def environment() -> dict[str, str]:
    """Allow-listed environment (C7 §6). Environment variables are never recorded."""
    env = {"python": platform.python_version(), "os": platform.system(), "device": "cpu"}
    for pkg in ("numpy", "rasterio", "pyproj", "shapely", "scipy"):
        try:
            env[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            continue
    try:
        import rasterio

        env["gdal"] = rasterio.__gdal_version__
    except Exception:  # pragma: no cover
        pass
    return env


def code_version() -> str:
    """Git SHA of the working tree, with ``-dirty`` when uncommitted changes exist."""
    root = Path(__file__).resolve().parents[4]
    try:
        sha = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=5
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True, check=True, timeout=5
        ).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
    except Exception:
        return "unknown"
