"""Provenance expansion (C7 §9): evidence → processing run → inputs → scenes → assets."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text

from aether.db import repo


def _assets(conn: Connection, scene_ids: list[Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(
        text(
            """SELECT s.item_id, a.asset_key, a.href, a.file_checksum FROM assets a JOIN scenes s ON s.id = a.scene_id
           WHERE a.scene_id = ANY(:ids) ORDER BY s.item_id, a.asset_key"""
        ),
        {"ids": scene_ids},
    ).mappings():
        out.setdefault(r["item_id"], []).append(
            {"asset_key": r["asset_key"], "href": r["href"], "file_checksum": r["file_checksum"]}
        )
    return out


def expand(conn: Connection, evidence_id: str) -> dict[str, Any]:
    ev = repo.get_evidence(conn, evidence_id)
    run = dict(
        conn.execute(
            text(
                """SELECT id, kind, algorithm_versions, parameters, parameters_hash, grid_hash, code_version, environment,
                  steps, timings, started_at, completed_at FROM processing_runs WHERE id=:id"""
            ),
            {"id": ev["processing_run_id"]},
        )
        .mappings()
        .one()
    )
    scenes: list[dict[str, Any]] = []
    if ev["analysis_run_id"] is not None:
        rows = (
            conn.execute(
                text(
                    """SELECT s.id, s.item_id, s.collection, s.acquired_at, s.platform, s.processing_baseline, s.stac_sha256,
                      i.status, i.reason_code, i.datatake_id
               FROM analysis_run_inputs i JOIN scenes s ON s.id = i.scene_id
               WHERE i.analysis_run_id = :a ORDER BY s.acquired_at, s.item_id"""
                ),
                {"a": ev["analysis_run_id"]},
            )
            .mappings()
            .all()
        )
        scenes = [dict(r) for r in rows]
    elif ev["kind"] in ("catalog_query", "scene_metadata"):
        ids = ev["facts"].get("item_ids") or [ev["facts"].get("item_id")]
        rows = (
            conn.execute(
                text(
                    """SELECT id, item_id, collection, acquired_at, platform, processing_baseline, stac_sha256
               FROM scenes WHERE item_id = ANY(:ids) ORDER BY acquired_at, item_id"""
                ),
                {"ids": ids},
            )
            .mappings()
            .all()
        )
        scenes = [dict(r) for r in rows]
    assets = _assets(conn, [s["id"] for s in scenes]) if scenes else {}
    for s in scenes:
        s["assets"] = assets.get(s["item_id"], [])
        s.pop("id", None)
    artifacts = [
        dict(r)
        for r in conn.execute(
            text(
                """SELECT id, sha256, kind, crs_epsg, transform, width, height, dtype, nodata, resolution_m, band_names
           FROM artifacts WHERE id = ANY(:ids)"""
            ),
            {"ids": ev["artifact_ids"]},
        ).mappings()
    ]
    return {
        "evidence_id": evidence_id,
        "kind": ev["kind"],
        "processing_run": run,
        "scenes": scenes,
        "artifacts": artifacts,
        "step_kinds": sorted({s["kind"] for s in run["steps"]}),
    }
