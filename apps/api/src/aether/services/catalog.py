"""Synchronous scene discovery (C1 §4–§6): search, eligibility, persistence, catalogue evidence."""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection

from aether import science
from aether.db import repo
from aether.domain.periods import Period
from aether.evidence.builders import EvidenceDraft, MeasurementDraft, canonical_hash
from aether.evidence.provenance import code_version, environment
from aether.geo.stac.client import SearchResult, StacSearcher
from aether.geo.stac.eligibility import SceneRecord, apply_eligibility, group_observations, parse_item

log = logging.getLogger(__name__)


def discover(
    searcher: StacSearcher, aoi: dict[str, Any], period: Period, max_tile_cloud: float = science.MAX_TILE_CLOUD_DEFAULT
) -> tuple[SearchResult, list[SceneRecord]]:
    """Search + parse + E1–E7. Pure apart from the STAC call."""
    result = searcher.search(aoi["geometry"], period.start, period.end, max_tile_cloud)
    scenes = [parse_item(it) for it in result.items]
    apply_eligibility(scenes, aoi["utm_epsg"])
    for s in scenes:
        if s.status == "excluded":
            log.info("scene.excluded item=%s reason=%s", s.item_id, s.reason_code)
    return result, scenes


def count_measurements(scenes: list[SceneRecord], number_matched: int | None) -> list[MeasurementDraft]:
    method = f"{science.INGEST_VERSION}@1"
    ms = [
        MeasurementDraft(
            "items_matched",
            "count",
            number_matched,
            method,
            unavailable_reason=None if number_matched is not None else "not_applicable",
        ),
        MeasurementDraft("items_considered", "count", len(scenes), method),
        MeasurementDraft("items_used", "count", sum(s.status == "used" for s in scenes), method),
    ]
    for reason, n in sorted(Counter(s.reason_code for s in scenes if s.status == "excluded" and s.reason_code).items()):
        ms.append(MeasurementDraft(f"items_excluded.{reason}", "count", n, method))
    return ms


def search_and_record(
    conn: Connection, searcher: StacSearcher, *, aoi_id: uuid.UUID, period: Period, max_tile_cloud: float
) -> dict[str, Any]:
    started = datetime.now(UTC)
    aoi = repo.get_aoi(conn, aoi_id)
    result, scenes = discover(searcher, aoi, period, max_tile_cloud)

    params = {"period": period.as_tuple(), "max_tile_cloud": max_tile_cloud, "collection": result.collection}
    ph = canonical_hash(params)
    run_id = repo.insert_processing_run(
        conn,
        project_id=aoi["project_id"],
        analysis_run_id=None,
        kind="ingest",
        parameters=params,
        parameters_hash=ph,
        grid_hash=None,
        code_version=code_version(),
        environment=environment(),
        steps=[
            {
                "kind": "catalog_search",
                "api_url": result.api_url,
                "collection": result.collection,
                "request": result.request,
                "item_ids": [s.item_id for s in scenes],
            }
        ],
        timings={},
        started_at=started,
    )
    search_id = repo.insert_scene_search(
        conn,
        project_id=aoi["project_id"],
        aoi_id=aoi_id,
        api_url=result.api_url,
        collection=result.collection,
        request=result.request,
        item_ids=[s.item_id for s in scenes],
        number_matched=result.number_matched,
    )
    for s in scenes:
        _, created = repo.upsert_scene(conn, s)
        if created:
            repo.insert_evidence(
                conn,
                scene_metadata_evidence(s),
                project_id=aoi["project_id"],
                analysis_run_id=None,
                processing_run_id=run_id,
                parameters_hash=ph,
            )
    ev = EvidenceDraft(
        kind="catalog_query",
        facts={
            "scene_search_id": str(search_id),
            "api_url": result.api_url,
            "collection": result.collection,
            "aoi_id": str(aoi_id),
            "item_ids": [s.item_id for s in scenes],
        },
        measurements=count_measurements(scenes, result.number_matched),
        geometry=aoi["geometry"],
        observed_period_a=period.as_tuple(),
    )
    ev_id = repo.insert_evidence(
        conn, ev, project_id=aoi["project_id"], analysis_run_id=None, processing_run_id=run_id, parameters_hash=ph
    )
    observations = group_observations(scenes)
    meta = repo.scene_metadata_measurements(conn, [s.item_id for s in scenes])
    return {
        "scene_search_id": search_id,
        "evidence_id": ev_id,
        "number_matched": result.number_matched,
        "observations_eligible": len(observations),
        "n_min": science.N_MIN.value,
        "scenes": [scene_summary(s, meta.get(s.item_id)) for s in scenes],
    }


def scene_metadata_evidence(s: SceneRecord) -> EvidenceDraft:
    method = f"{science.INGEST_VERSION}@1"
    cloud = None if s.tile_cloud_cover is None else s.tile_cloud_cover / 100.0
    return EvidenceDraft(
        kind="scene_metadata",
        facts={
            "item_id": s.item_id,
            "collection": s.collection,
            "platform": s.platform,
            "acquired_at": s.acquired_at.isoformat() if s.acquired_at else None,
            "processing_baseline": s.processing_baseline,
            "datatake_id": s.datatake_id,
            "grid_code": s.grid_code,
            "stac_sha256": s.stac_sha256,
            "cloud_cover_scope": "tile-level (STAC eo:cloud_cover), not AOI cloud cover",
        },
        measurements=[
            MeasurementDraft(
                "tile_cloud_cover",
                "value",
                cloud,
                method,
                region="none",
                unavailable_reason=None if cloud is not None else "not_applicable",
            ),
            MeasurementDraft(
                "sun_elevation",
                "value",
                s.sun_elevation_deg,
                method,
                region="none",
                unavailable_reason=None if s.sun_elevation_deg is not None else "not_applicable",
            ),
        ],
        geometry=s.footprint,
    )


def scene_summary(s: SceneRecord, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Scene row for the UI. Numbers appear only as Measurement objects of scene_metadata evidence."""
    return {
        "item_id": s.item_id,
        "acquired_at": s.acquired_at.isoformat() if s.acquired_at else None,
        "platform": s.platform,
        "processing_baseline": s.processing_baseline,
        "datatake_id": s.datatake_id,
        "grid_code": s.grid_code,
        "metadata_evidence": metadata,  # {evidence_id, measurements: [...]} incl. tile-level cloud cover
        "status": s.status,
        "reason_code": s.reason_code,
    }
