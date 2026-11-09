"""Read model for persisted change events: ``GET /changes`` (plan §12, BACKEND-DESIGN).

Only persisted truth is returned. Nothing here runs or re-derives the change engine:
- features come from ``change_events`` + their ``change_event`` evidence, one feature per event,
  ordered by the global ``event_label`` (C-17). Overlapping events of different categories stay
  separate features; no merging, clipping or simplification;
- the geometry is the evidence's EPSG:4326 copy. The authoritative geometry is the UTM pixel-edge
  ``change_events.gpkg`` named by ``geometry_artifact_id`` (feature id = ``event_label``, C5 §10);
- properties carry the measurement objects (incl. the C-18 sign counts), the stored confidence band,
  ``index_flags``, limitations and identifiers.
"""

from __future__ import annotations

import math
import uuid
from typing import Any

from sqlalchemy import Connection, text

from aether.db import repo
from aether.domain.errors import AetherError, NotFound
from aether.domain.registries import LIMITATIONS
from aether.geo.change.rules import CATEGORIES

CATEGORY_NAMES = frozenset(CATEGORIES.values())


def parse_bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
    """``minLon,minLat,maxLon,maxLat`` in EPSG:4326 (RFC 7946 order)."""
    if bbox is None:
        return None
    try:
        parts = tuple(float(v) for v in bbox.split(","))
    except ValueError as exc:
        raise AetherError("bbox_invalid", "bbox must be minLon,minLat,maxLon,maxLat") from exc
    if len(parts) != 4 or not all(math.isfinite(v) for v in parts):
        raise AetherError("bbox_invalid", "bbox must be four finite numbers minLon,minLat,maxLon,maxLat")
    minx, miny, maxx, maxy = parts
    if not (-180 <= minx < maxx <= 180 and -90 <= miny < maxy <= 90):
        raise AetherError("bbox_invalid", "bbox must satisfy -180 ≤ minLon < maxLon ≤ 180 and -90 ≤ minLat < maxLat ≤ 90")
    return minx, miny, maxx, maxy


def feature_collection(conn: Connection, *, analysis_run_id: uuid.UUID, bbox: str | None, category: str | None) -> dict[str, Any]:
    run = (
        conn.execute(text("SELECT id, type, status, outcome FROM analysis_runs WHERE id = :id"), {"id": analysis_run_id})
        .mappings()
        .first()
    )
    if run is None:
        raise NotFound("analysis", str(analysis_run_id))
    if run["type"] != "change":
        raise AetherError("analysis_type_invalid", "change events exist only for change analyses")
    if category is not None and category not in CATEGORY_NAMES:
        raise AetherError("category_invalid", f"category must be one of {sorted(CATEGORY_NAMES)}")
    box = parse_bbox(bbox)
    if run["status"] != "succeeded":
        # Not "no change": the run has not produced results (yet). An empty collection would be misleading.
        raise AetherError(
            "analysis_not_completed",
            f"the change analysis is {run['status']}; events exist only for succeeded runs",
            http_status=409,
            details={"status": run["status"]},
        )
    features = []
    for row in repo.change_event_rows(conn, analysis_run_id, box, category):
        facts = row["facts"]
        features.append(
            {
                "type": "Feature",
                "id": facts["event_label"],
                "geometry": row["geometry"],
                "properties": {
                    "evidence_id": row["evidence_id"],
                    "event_label": facts["event_label"],
                    "category": row["category"],
                    "touches_aoi_boundary": row["touches_aoi_boundary"],
                    "confidence_band": row["confidence_band"],
                    "index_flags": facts["index_flags"],
                    "mode": facts["mode"],
                    "geometry_artifact_id": row["geometry_artifact_id"],
                    "limitations": [{**lim, "text": LIMITATIONS[lim["code"]].text} for lim in row["limitations"]],
                    "measurements": row["measurements"],
                },
            }
        )
    return {
        "type": "FeatureCollection",
        "analysis_run_id": analysis_run_id,
        "outcome": run["outcome"],
        "feature_count": len(features),
        "features": features,
    }
