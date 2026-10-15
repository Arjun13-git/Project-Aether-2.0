"""AOI creation: C3 §2 admissibility + immutable row + ``aoi_measurement`` evidence (C7)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection

from aether import science
from aether.db import repo
from aether.evidence.builders import EvidenceDraft, MeasurementDraft, canonical_hash
from aether.evidence.provenance import code_version, environment
from aether.geo.aoi import normalize_aoi
from aether.geo.grid import build_analysis_grid

log = logging.getLogger(__name__)


def create_aoi(conn: Connection, *, project_id: uuid.UUID, name: str, geometry: dict[str, Any], source: str) -> dict[str, Any]:
    repo.get_project(conn, project_id)
    started = datetime.now(UTC)
    aoi = normalize_aoi(geometry)
    grid = build_analysis_grid(aoi.geometry, aoi.utm_epsg)  # rejects aoi_extent_too_large (C3 §2)

    existing = repo.find_aoi(conn, project_id, aoi.geometry_hash, name)
    if existing is not None:
        return repo.get_aoi(conn, existing)  # AOIs are immutable: same geometry + name = same AOI

    aoi_id = repo.insert_aoi(
        conn,
        project_id=project_id,
        name=name,
        geojson=aoi.geojson(),
        geometry_hash=aoi.geometry_hash,
        source=source,
        source_geojson=geometry,
        normalization=aoi.operations,
        utm_epsg=aoi.utm_epsg,
    )
    params = {"area_method": science.GEODESIC_AREA_METHOD}
    run_id = repo.insert_processing_run(
        conn,
        project_id=project_id,
        analysis_run_id=None,
        kind="aoi_measure",
        parameters=params,
        parameters_hash=canonical_hash(params),
        grid_hash=grid.grid_hash,
        code_version=code_version(),
        environment=environment(),
        steps=[
            {
                "kind": "aoi_normalization",
                "operations": aoi.operations,
                "geometry_hash": aoi.geometry_hash,
                "area_method": science.GEODESIC_AREA_METHOD,
                "ellipsoid": "WGS84",
            }
        ],
        timings={},
        started_at=started,
    )
    ev = EvidenceDraft(
        kind="aoi_measurement",
        facts={
            "aoi_id": str(aoi_id),
            "name": name,
            "geometry_hash": aoi.geometry_hash,
            "utm_epsg": aoi.utm_epsg,
            "crs": "EPSG:4326",
            "normalization": aoi.operations,
            "grid_hash": grid.grid_hash,
        },
        measurements=[MeasurementDraft("aoi_area", "value", aoi.area_m2, science.GEODESIC_AREA_METHOD)],
        geometry=aoi.geojson(),
    )
    repo.insert_evidence(
        conn,
        ev,
        project_id=project_id,
        analysis_run_id=None,
        processing_run_id=run_id,
        parameters_hash=canonical_hash(params),
    )
    log.info("aoi.created id=%s epsg=%s ops=%s", aoi_id, aoi.utm_epsg, aoi.operations)
    out = repo.get_aoi(conn, aoi_id)
    out["grid"] = {"G10": grid.g10.describe(), "G20": grid.g20.describe()}
    return out
