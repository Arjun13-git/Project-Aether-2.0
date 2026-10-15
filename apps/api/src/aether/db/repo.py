"""Repositories: explicit SQL for every write and read the services need."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, text

from aether import science
from aether.domain.errors import NotFound
from aether.evidence.builders import EvidenceDraft, content_sha256, unit_of, validate_evidence
from aether.evidence.provenance import validate_steps
from aether.geo.stac.eligibility import SceneRecord


def _j(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


# --- projects / AOIs -----------------------------------------------------------------------------


def insert_project(conn: Connection, name: str, description: str) -> dict[str, Any]:
    row = (
        conn.execute(
            text("INSERT INTO projects (name, description) VALUES (:n, :d) RETURNING id, name, description, created_at"),
            {"n": name, "d": description},
        )
        .mappings()
        .one()
    )
    return dict(row)


def list_projects(conn: Connection) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(text("SELECT id, name, description, created_at FROM projects ORDER BY created_at DESC")).mappings()
    ]


def get_project(conn: Connection, project_id: uuid.UUID) -> dict[str, Any]:
    row = (
        conn.execute(text("SELECT id, name, description, created_at FROM projects WHERE id = :id"), {"id": project_id})
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("project", str(project_id))
    return dict(row)


def insert_aoi(
    conn: Connection,
    *,
    project_id: uuid.UUID,
    name: str,
    geojson: dict[str, Any],
    geometry_hash: str,
    source: str,
    source_geojson: dict[str, Any],
    normalization: list[str],
    utm_epsg: int,
) -> uuid.UUID:
    return conn.execute(
        text(
            """INSERT INTO aois (project_id, name, geom, geometry_hash, source, source_geojson, normalization, utm_epsg)
           VALUES (:p, :n, ST_SetSRID(ST_GeomFromGeoJSON(:g), 4326), :h, :s, CAST(:sg AS jsonb), :norm, :e)
           RETURNING id"""
        ),
        {
            "p": project_id,
            "n": name,
            "g": _j(geojson),
            "h": geometry_hash,
            "s": source,
            "sg": _j(source_geojson),
            "norm": normalization,
            "e": utm_epsg,
        },
    ).scalar_one()


def find_aoi(conn: Connection, project_id: uuid.UUID, geometry_hash: str, name: str) -> uuid.UUID | None:
    return conn.execute(
        text("SELECT id FROM aois WHERE project_id=:p AND geometry_hash=:h AND name=:n"),
        {"p": project_id, "h": geometry_hash, "n": name},
    ).scalar()


def get_aoi(conn: Connection, aoi_id: uuid.UUID) -> dict[str, Any]:
    row = (
        conn.execute(
            text(
                """SELECT a.id, a.project_id, a.name, ST_AsGeoJSON(a.geom, 9)::jsonb AS geometry, a.geometry_hash, a.source,
                  a.normalization, a.utm_epsg, a.created_at,
                  (SELECT e.evidence_id FROM evidence e WHERE e.kind='aoi_measurement'
                     AND e.facts->>'aoi_id' = a.id::text ORDER BY e.created_at LIMIT 1) AS evidence_id
           FROM aois a WHERE a.id = :id"""
            ),
            {"id": aoi_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("aoi", str(aoi_id))
    return dict(row)


def list_aois(conn: Connection, project_id: uuid.UUID) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            text(
                """SELECT id, name, ST_AsGeoJSON(geom, 9)::jsonb AS geometry, geometry_hash, utm_epsg, created_at
           FROM aois WHERE project_id=:p ORDER BY created_at DESC"""
            ),
            {"p": project_id},
        ).mappings()
    ]


# --- scenes ----------------------------------------------------------------------------------------


def upsert_scene(conn: Connection, s: SceneRecord) -> tuple[uuid.UUID, bool]:
    """Insert a scene and its six consumed assets once; returns (scene_id, created)."""
    existing = conn.execute(
        text("SELECT id FROM scenes WHERE provider='earth-search' AND collection=:c AND item_id=:i"),
        {"c": s.collection, "i": s.item_id},
    ).scalar()
    if existing is not None:
        return existing, False
    sid = conn.execute(
        text(
            """INSERT INTO scenes (provider, collection, item_id, stac_json, stac_sha256, stac_updated, acquired_at,
             platform, processing_baseline, datatake_id, grid_code, epsg, tile_cloud_cover, sun_elevation_deg,
             sun_azimuth_deg, incidence_angle_deg, footprint)
           VALUES ('earth-search', :c, :i, CAST(:j AS jsonb), :sha, :upd, :acq, :plat, :bl, :dt, :gc, :epsg, :cc,
             :se, :sa, :ia,
             CASE WHEN CAST(:fp AS text) IS NULL THEN NULL
               ELSE ST_SetSRID(ST_GeomFromGeoJSON(CAST(:fp AS text)), 4326) END)
           RETURNING id"""
        ),
        {
            "c": s.collection,
            "i": s.item_id,
            "j": _j(s.stac_json),
            "sha": s.stac_sha256,
            "upd": s.stac_updated,
            "acq": s.acquired_at,
            "plat": s.platform,
            "bl": s.processing_baseline,
            "dt": s.datatake_id,
            "gc": s.grid_code,
            "epsg": s.epsg,
            "cc": s.tile_cloud_cover,
            "se": s.sun_elevation_deg,
            "sa": s.sun_azimuth_deg,
            "ia": s.incidence_angle_deg,
            "fp": _j(s.footprint) if s.footprint else None,
        },
    ).scalar_one()
    for a in s.assets.values():
        conn.execute(
            text(
                """INSERT INTO assets (scene_id, asset_key, href, media_type, raster_bands, file_checksum, file_size,
                 proj_transform, proj_shape)
               VALUES (:s, :k, :h, :m, CAST(:rb AS jsonb), :ck, :sz, :pt, :ps)"""
            ),
            {
                "s": sid,
                "k": a.key,
                "h": a.href,
                "m": a.media_type,
                "rb": _j(a.raster_bands),
                "ck": a.file_checksum,
                "sz": a.file_size,
                "pt": a.proj_transform,
                "ps": a.proj_shape,
            },
        )
    return sid, True


def insert_scene_search(
    conn: Connection,
    *,
    project_id: uuid.UUID,
    aoi_id: uuid.UUID,
    api_url: str,
    collection: str,
    request: dict[str, Any],
    item_ids: list[str],
    number_matched: int | None,
) -> uuid.UUID:
    return conn.execute(
        text(
            """INSERT INTO scene_searches (project_id, aoi_id, api_url, collection, request, item_ids, number_matched)
           VALUES (:p, :a, :u, :c, CAST(:r AS jsonb), :ids, :nm) RETURNING id"""
        ),
        {
            "p": project_id,
            "a": aoi_id,
            "u": api_url,
            "c": collection,
            "r": _j(request),
            "ids": item_ids,
            "nm": number_matched,
        },
    ).scalar_one()


# --- provenance / artifacts / evidence -------------------------------------------------------------


def insert_processing_run(
    conn: Connection,
    *,
    project_id: uuid.UUID,
    analysis_run_id: uuid.UUID | None,
    kind: str,
    parameters: dict[str, Any],
    parameters_hash: str,
    grid_hash: str | None,
    code_version: str,
    environment: dict[str, Any],
    steps: list[dict[str, Any]],
    timings: dict[str, float],
    started_at: datetime,
    algorithm_versions: dict[str, str],
) -> uuid.UUID:
    """Insert a processing run. ``algorithm_versions`` is explicit per run type (snapshot vs change, C-10)."""
    validate_steps(steps)
    return conn.execute(
        text(
            """INSERT INTO processing_runs (project_id, analysis_run_id, kind, algorithm_versions, parameters,
             parameters_hash, grid_hash, code_version, environment, steps, timings, started_at, completed_at)
           VALUES (:p, :a, :k, CAST(:av AS jsonb), CAST(:par AS jsonb), :ph, :gh, :cv, CAST(:env AS jsonb),
             CAST(:st AS jsonb), CAST(:tm AS jsonb), :sa, :ca) RETURNING id"""
        ),
        {
            "p": project_id,
            "a": analysis_run_id,
            "k": kind,
            "av": _j(algorithm_versions),
            "par": _j(parameters),
            "ph": parameters_hash,
            "gh": grid_hash,
            "cv": code_version,
            "env": _j(environment),
            "st": _j(steps),
            "tm": _j(timings),
            "sa": started_at,
            "ca": datetime.now(UTC),
        },
    ).scalar_one()


def insert_artifact(
    conn: Connection,
    *,
    run_id: uuid.UUID,
    sha256: str,
    uri: str,
    media_type: str,
    nbytes: int,
    kind: str,
    raster: dict[str, Any],
) -> uuid.UUID:
    existing = conn.execute(text("SELECT id FROM artifacts WHERE sha256=:s"), {"s": sha256}).scalar()
    if existing is not None:
        return existing  # content-addressed: identical bytes are one artifact (first producer recorded)
    return conn.execute(
        text(
            """INSERT INTO artifacts (sha256, uri, media_type, bytes, kind, crs_epsg, transform, width, height, dtype,
             nodata, resolution_m, band_names, produced_by_run_id)
           VALUES (:sha, :uri, :mt, :b, :k, :epsg, :tr, :w, :h, :dt, :nd, :res, :bn, :run) RETURNING id"""
        ),
        {
            "sha": sha256,
            "uri": uri,
            "mt": media_type,
            "b": nbytes,
            "k": kind,
            "epsg": raster.get("crs_epsg"),
            "tr": raster.get("transform"),
            "w": raster.get("width"),
            "h": raster.get("height"),
            "dt": raster.get("dtype"),
            "nd": raster.get("nodata"),
            "res": raster.get("resolution_m"),
            "bn": raster.get("band_names"),
            "run": run_id,
        },
    ).scalar_one()


def insert_evidence(
    conn: Connection,
    ev: EvidenceDraft,
    *,
    project_id: uuid.UUID,
    analysis_run_id: uuid.UUID | None,
    processing_run_id: uuid.UUID,
    parameters_hash: str,
    artifact_ids: dict[str, uuid.UUID] | None = None,
) -> str:
    validate_evidence(ev)
    ids = [artifact_ids[k] for k in ev.artifact_keys] if (ev.artifact_keys and artifact_ids) else []
    if ev.artifact_keys and not artifact_ids:
        raise ValueError("evidence references artifacts but none were provided")
    period = f"[{ev.observed_period_a[0]},{ev.observed_period_a[1]}]" if ev.observed_period_a else None
    conn.execute(
        text(
            """INSERT INTO evidence (evidence_id, schema_version, project_id, analysis_run_id, processing_run_id, kind,
             geom, observed_period_a, facts, confidence_band, limitations, artifact_ids, content_sha256)
           VALUES (:id, :sv, :p, :a, :pr, :k,
             CASE WHEN CAST(:g AS text) IS NULL THEN NULL ELSE ST_SetSRID(ST_GeomFromGeoJSON(CAST(:g AS text)), 4326) END,
             CAST(:pa AS daterange), CAST(:f AS jsonb), 'n/a', CAST(:l AS jsonb), CAST(:art AS uuid[]), :h)"""
        ),
        {
            "id": ev.evidence_id,
            "sv": science.EVIDENCE_SCHEMA_VERSION,
            "p": project_id,
            "a": analysis_run_id,
            "pr": processing_run_id,
            "k": ev.kind,
            "g": _j(ev.geometry) if ev.geometry else None,
            "pa": period,
            "f": _j(ev.facts),
            "l": _j([{"code": c, "blocking": _blocking(c)} for c in ev.limitations]),
            "art": [str(i) for i in ids],
            "h": content_sha256(ev, str(processing_run_id)),
        },
    )
    by_q = {m.quantity: m.measurement_id for m in ev.measurements}
    for m in ev.measurements:
        conn.execute(
            text(
                """INSERT INTO measurements (measurement_id, evidence_id, quantity, statistic, status, value,
                 unavailable_reason, unit, denominator_quantity, denominator_measurement_id, n_samples, region,
                 method, parameters_hash, processing_run_id)
               VALUES (:id, :ev, :q, :st, :status, :v, :r, :u, :dq, :dm, :n, :reg, :meth, :ph, :pr)"""
            ),
            {
                "id": m.measurement_id,
                "ev": ev.evidence_id,
                "q": m.quantity,
                "st": m.statistic,
                "status": m.status,
                "v": None if m.value is None else float(m.value),
                "r": m.unavailable_reason,
                "u": unit_of(m.quantity),
                "dq": m.denominator,
                "dm": by_q.get(m.denominator) if m.denominator else None,
                "n": m.n_samples,
                "reg": m.region,
                "meth": m.method,
                "ph": parameters_hash,
                "pr": processing_run_id,
            },
        )
    return ev.evidence_id


def _blocking(code: str) -> bool:
    from aether.domain.registries import LIMITATIONS

    return LIMITATIONS[code].blocking


def get_evidence(conn: Connection, evidence_id: str) -> dict[str, Any]:
    row = (
        conn.execute(
            text(
                """SELECT evidence_id, schema_version, project_id, analysis_run_id, processing_run_id, kind,
                  ST_AsGeoJSON(geom, 9)::jsonb AS geometry, lower(observed_period_a) AS period_start,
                  upper(observed_period_a) - 1 AS period_end, facts, confidence_band, limitations, artifact_ids,
                  content_sha256, created_at
           FROM evidence WHERE evidence_id = :id"""
            ),
            {"id": evidence_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("evidence", evidence_id)
    ev = dict(row)
    ev["measurements"] = [
        dict(m)
        for m in conn.execute(
            text(
                """SELECT measurement_id, quantity, statistic, status, value, unavailable_reason, unit, denominator_quantity,
                  denominator_measurement_id, n_samples, region, method, parameters_hash
           FROM measurements WHERE evidence_id = :id ORDER BY quantity, statistic"""
            ),
            {"id": evidence_id},
        ).mappings()
    ]
    return ev


def scene_metadata_measurements(conn: Connection, item_ids: list[str]) -> dict[str, dict[str, Any]]:
    """item_id → {evidence_id, measurements[]} of the scene_metadata evidence for each scene."""
    if not item_ids:
        return {}
    rows = (
        conn.execute(
            text(
                """SELECT DISTINCT ON (e.facts->>'item_id') e.facts->>'item_id' AS item_id, e.evidence_id
           FROM evidence e WHERE e.kind = 'scene_metadata' AND e.facts->>'item_id' = ANY(:ids)
           ORDER BY e.facts->>'item_id', e.created_at"""
            ),
            {"ids": item_ids},
        )
        .mappings()
        .all()
    )
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        ms = conn.execute(
            text(
                """SELECT measurement_id, quantity, statistic, status, value, unavailable_reason, unit
               FROM measurements WHERE evidence_id = :e ORDER BY quantity"""
            ),
            {"e": r["evidence_id"]},
        ).mappings()
        out[r["item_id"]] = {"evidence_id": r["evidence_id"], "measurements": [dict(m) for m in ms]}
    return out


def run_artifacts(conn: Connection, analysis_run_id: uuid.UUID) -> list[dict[str, Any]]:
    """Artifacts of a run: those cited by its evidence + its display composite (provenance step).

    ``artifacts.produced_by_run_id`` records only the first producer of identical bytes, so it must
    not be used to find a run's outputs.
    """
    rows = conn.execute(
        text(
            """SELECT DISTINCT a.id, a.kind, a.band_names, a.resolution_m, a.sha256
               FROM evidence e JOIN artifacts a ON a.id = ANY(e.artifact_ids)
               WHERE e.analysis_run_id = :r
               UNION
               SELECT a.id, a.kind, a.band_names, a.resolution_m, a.sha256
               FROM processing_runs p, jsonb_array_elements(p.steps) st JOIN artifacts a ON a.sha256 = st->>'artifact_sha256'
               WHERE p.analysis_run_id = :r AND st->>'kind' = 'display_composite'
               UNION
               SELECT a.id, a.kind, a.band_names, a.resolution_m, a.sha256
               FROM artifacts a JOIN processing_runs p ON p.id = a.produced_by_run_id
               WHERE p.analysis_run_id = :r
               ORDER BY kind, band_names"""
        ),
        {"r": analysis_run_id},
    ).mappings()
    return [dict(r) for r in rows]
