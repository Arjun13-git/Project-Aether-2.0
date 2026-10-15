"""Evidence gate suite (C7 §9): DB CHECKs and immutability triggers."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

pytestmark = pytest.mark.db


def _seed(conn):
    pid = conn.execute(text("INSERT INTO projects (name) VALUES ('t') RETURNING id")).scalar_one()
    pr = conn.execute(
        text(
            """INSERT INTO processing_runs (project_id, kind, algorithm_versions, parameters, parameters_hash, code_version,
             environment, steps, started_at, completed_at)
           VALUES (:p, 'aoi_measure', '{}', '{}', 'h', 'sha', '{}', '[]', now(), now()) RETURNING id"""
        ),
        {"p": pid},
    ).scalar_one()
    conn.execute(
        text(
            """INSERT INTO evidence (evidence_id, schema_version, project_id, processing_run_id, kind, facts,
             confidence_band, limitations, content_sha256)
           VALUES ('ev_' || repeat('a', 32), 'evidence.v1', :p, :pr, 'aoi_measurement', '{}', 'n/a', '[]', 'x')"""
        ),
        {"p": pid, "pr": pr},
    )
    return pid, pr


def _measurement(conn, pr, **over):
    row = {
        "id": "ms_" + "b" * 32,
        "q": "aoi_area",
        "st": "value",
        "status": "available",
        "v": 1.0,
        "r": None,
        "u": "m2",
        "dq": None,
        "dm": None,
        "pr": pr,
    }
    row.update(over)
    conn.execute(
        text(
            """INSERT INTO measurements (measurement_id, evidence_id, quantity, statistic, status, value,
             unavailable_reason, unit, denominator_quantity, denominator_measurement_id, region, method,
             parameters_hash, processing_run_id)
           VALUES (:id, 'ev_' || repeat('a', 32), :q, :st, :status, :v, :r, :u, :dq, :dm, 'aoi', 'geodesic_area@1',
             'h', :pr)"""
        ),
        row,
    )


@pytest.mark.parametrize(
    "over",
    [
        {"st": "fraction", "u": "1"},  # fraction without denominator
        {"v": None},  # null value with available status
        {"status": "unavailable", "v": None},  # unavailable without reason
        {"status": "unavailable", "v": 0.0, "r": "insufficient_support"},  # unavailable stored as 0
        {"u": "km2"},  # unknown unit
        {"v": float("nan")},  # NaN
        {"r": "insufficient_support"},  # available with a reason
    ],
)
def test_measurement_checks_reject(clean, over):
    with pytest.raises((IntegrityError, DBAPIError)), clean.begin() as conn:
        _, pr = _seed(conn)
        _measurement(conn, pr, **over)


def test_valid_measurement_accepted_then_immutable(clean):
    with clean.begin() as conn:
        _, pr = _seed(conn)
        _measurement(conn, pr)
    for sql in (
        "UPDATE measurements SET value = 2",
        "DELETE FROM measurements",
        'UPDATE evidence SET facts = \'{"x":"y"}\'',
        "DELETE FROM evidence",
    ):
        with pytest.raises(DBAPIError) as e, clean.begin() as conn:
            conn.execute(text(sql))
        assert "immutable_row" in str(e.value)


def test_project_delete_cascades_only_with_explicit_flag(clean):
    with clean.begin() as conn:
        pid, pr = _seed(conn)
        _measurement(conn, pr)
    with pytest.raises(DBAPIError), clean.begin() as conn:
        conn.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})
    with clean.begin() as conn:
        conn.execute(text("SET LOCAL aether.project_delete = 'on'"))
        conn.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})
        assert conn.execute(text("SELECT count(*) FROM evidence")).scalar_one() == 0


@pytest.mark.parametrize(
    "bbox", [(-121.80, 38.52, -121.77, 38.545), (149.10, -35.30, 149.13, -35.27), (18.0, 69.6, 18.05, 69.63)]
)
def test_geodesic_area_agrees_with_postgis_geography(clean, bbox):
    """Area gate (testing strategy): pyproj geodesic area vs PostGIS geography, < 0.01 %."""
    from shapely.geometry import box

    from aether.geo.aoi import geodesic_area_m2

    geom = box(*bbox)
    with clean.connect() as conn:
        pg = conn.execute(text("SELECT ST_Area(ST_GeomFromText(:w, 4326)::geography, true)"), {"w": geom.wkt}).scalar_one()
    assert abs(geodesic_area_m2(geom) - pg) / pg < 1e-4
