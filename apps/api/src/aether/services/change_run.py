"""Execute one Phase 2 change analysis in memory: discover A, discover B, run the change engine.

Self-contained (C-07):

    ChangeRunRequest ──┬── Period A: discover (catalogue + E1–E7) ── run_period(period="a")
                       ├── Period B: discover (catalogue + E1–E7) ── run_period(period="b")
                       └── co-registration → joint_valid → Δ → reference → significance
                           → categories → morphology/MMU → polygons → ChangeRunResult

No snapshot run, snapshot record or foreign key is read or required. A period that a snapshot run
already processed is reused only through the window cache (``(grid_hash, item_id, asset_key)``,
JOB-SYSTEM) and content-addressed artifacts (identical bytes → identical SHA-256).

This stage has no persistence: the database rows, artifacts and evidence of a change run are later
stages. The module does not import the database layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from shapely.geometry import shape

from aether import science
from aether.domain.errors import AetherError
from aether.domain.periods import Period, validate_pair
from aether.evidence.builders import canonical_hash
from aether.evidence.provenance import validate_steps
from aether.geo.change.engine import ChangeAnalysis, run_change
from aether.geo.grid import build_analysis_grid
from aether.geo.io.windows import AssetReader
from aether.geo.period import CancelFn, ProgressFn
from aether.geo.stac.client import SearchResult, StacSearcher
from aether.services.catalog import discover


@dataclass(frozen=True)
class ChangeRunRequest:
    """AOI + two periods. There are no parameter overrides in Phase 2.0 (C-09)."""

    aoi: dict[str, Any]  # id, geometry_hash, geometry (GeoJSON, EPSG:4326), utm_epsg
    period_a: Period
    period_b: Period

    def __post_init__(self) -> None:
        missing = {"id", "geometry_hash", "geometry", "utm_epsg"} - set(self.aoi)
        if missing:
            raise AetherError("aoi_invalid", f"AOI record lacks {sorted(missing)}")


@dataclass(frozen=True)
class ChangeRunResult:
    request: ChangeRunRequest
    idempotency_key: str
    algorithm_versions: dict[str, str]  # CHANGE_ALGORITHM_VERSIONS (never the snapshot map)
    parameters: dict[str, object]  # change_parameters() (frozen defaults)
    search_a: SearchResult
    search_b: SearchResult
    analysis: ChangeAnalysis
    steps: list[dict[str, Any]]  # catalog_search ×2 + engine steps, every one schema-validated

    @property
    def outcome(self) -> str:
        return self.analysis.outcome


def change_idempotency_key(aoi: dict[str, Any], period_a: Period, period_b: Period) -> str:
    """Implementation plan §10 / JOB-SYSTEM C-10. Independent of the snapshot key, which is unchanged."""
    return canonical_hash(
        {
            "aoi_id": str(aoi["id"]),
            "geometry_hash": aoi["geometry_hash"],
            "type": "change",
            "period_a": period_a.as_tuple(),
            "period_b": period_b.as_tuple(),
            "collection": science.STAC_COLLECTION,
            "parameters": science.change_parameters(),
            "algorithm_versions": science.CHANGE_ALGORITHM_VERSIONS,
        }
    )


def _search_step(search: SearchResult, scene_ids: list[str], period: str) -> dict[str, Any]:
    return {
        "kind": "catalog_search",
        "api_url": search.api_url,
        "collection": search.collection,
        "request": search.request,
        "item_ids": scene_ids,
        "period": period,
    }


def execute_change(
    request: ChangeRunRequest,
    *,
    searcher: StacSearcher,
    reader: AssetReader,
    progress: ProgressFn | None = None,
    cancelled: CancelFn | None = None,
) -> ChangeRunResult:
    validate_pair(request.period_a, request.period_b)  # C4 §1 pair rules
    aoi = request.aoi
    grid = build_analysis_grid(shape(aoi["geometry"]), aoi["utm_epsg"])
    max_cloud = science.MAX_TILE_CLOUD_DEFAULT
    search_a, scenes_a = discover(searcher, aoi, request.period_a, max_cloud)
    search_b, scenes_b = discover(searcher, aoi, request.period_b, max_cloud)
    analysis = run_change(grid=grid, scenes_a=scenes_a, scenes_b=scenes_b, reader=reader, progress=progress, cancelled=cancelled)
    steps = [
        _search_step(search_a, [s.item_id for s in scenes_a], "a"),
        _search_step(search_b, [s.item_id for s in scenes_b], "b"),
        *analysis.steps,
    ]
    validate_steps(steps)
    return ChangeRunResult(
        request=request,
        idempotency_key=change_idempotency_key(aoi, request.period_a, request.period_b),
        algorithm_versions=dict(science.CHANGE_ALGORITHM_VERSIONS),
        parameters=science.change_parameters(),
        search_a=search_a,
        search_b=search_b,
        analysis=analysis,
        steps=steps,
    )
