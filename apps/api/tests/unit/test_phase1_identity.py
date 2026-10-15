"""Phase 1 identity boundary (Phase 2 gate G2).

These tests pin externally observable Phase 1 snapshot identity so that Phase 2 work (version-map
separation, per-period engine extraction) cannot change it silently:
- A: the snapshot idempotency key of a fixed reference request;
- B: the snapshot algorithm-version list;
- C: the six default snapshot parameters;
- D: engine fingerprints (COG bytes, array bytes, provenance steps, statistics) for a synthetic
  scenario and for real Sentinel-2 fixture windows.

The expected values were captured from the frozen Phase 1 code at `c7c9538` and are the regression
oracle. They must never be regenerated to make a refactor pass.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date
from pathlib import Path

import pytest

from aether import science
from aether.domain.periods import Period
from aether.services.analyses import idempotency_key

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "tiny" / "evidence" / "phase1_snapshot_golden.json"

REFERENCE_KEY = "3b2f1c793cda807ab850f3d4e7633f19f44c0b7e2aad6a0309e2ebebc71f6f0a"
REFERENCE_AOI = {"id": uuid.UUID("00000000-0000-0000-0000-000000000001"), "geometry_hash": "0" * 64}
REFERENCE_PERIOD = Period(date(2024, 6, 1), date(2024, 8, 15))

PHASE1_VERSIONS = [
    ("ingest", "ingest.earth_search.v1"),
    ("radiometry", "radiometry.s2c1.v1"),
    ("grid", "grid.utm_s2.v1"),
    ("mask", "mask.scl.v1"),
    ("composite", "composite.median_index.v1"),
    ("indices", "indices.s2.v1"),
    ("coverage", "coverage.v1"),
    ("zonal_stats", "zonal_stats@1"),
]
PHASE1_PARAMETERS = {
    "n_min": 3,
    "max_tile_cloud": 80.0,
    "mask_profile": "mask.scl.v1",
    "indices": ["mndwi", "ndbi", "ndvi", "ndwi"],
    "stats_min_support_fraction": 0.5,
    "coverage_thresholds": [0.5, 0.8],
}


# --- A: cache key ---------------------------------------------------------------------------------


def test_a_snapshot_cache_key_is_pinned():
    key = idempotency_key(REFERENCE_AOI, "snapshot", REFERENCE_PERIOD, science.snapshot_parameters())
    assert key == REFERENCE_KEY


def test_a_snapshot_cache_key_is_stable_for_the_same_request():
    params = science.snapshot_parameters()
    assert idempotency_key(REFERENCE_AOI, "snapshot", REFERENCE_PERIOD, params) == idempotency_key(
        REFERENCE_AOI, "snapshot", REFERENCE_PERIOD, dict(params)
    )


# --- B: version list ------------------------------------------------------------------------------


def test_b_snapshot_version_list_is_pinned():
    assert list(science.ALGORITHM_VERSIONS.items()) == PHASE1_VERSIONS
    assert json.dumps(science.ALGORITHM_VERSIONS, separators=(",", ":")) == json.dumps(
        dict(PHASE1_VERSIONS), separators=(",", ":")
    )


def test_b_snapshot_versions_contain_no_phase2_entries():
    values = " ".join(science.ALGORITHM_VERSIONS.values())
    for marker in ("change", "coreg", "gpkg", "vector"):
        assert marker not in values and marker not in science.ALGORITHM_VERSIONS


# --- C: parameters --------------------------------------------------------------------------------


def test_c_snapshot_parameters_are_pinned():
    params = science.snapshot_parameters()
    assert params == PHASE1_PARAMETERS
    assert list(params) == list(PHASE1_PARAMETERS)  # raw insertion order
    assert {k: type(v) for k, v in params.items()} == {k: type(v) for k, v in PHASE1_PARAMETERS.items()}
    assert json.dumps(params, sort_keys=True, separators=(",", ":")) == (
        '{"coverage_thresholds":[0.5,0.8],"indices":["mndwi","ndbi","ndvi","ndwi"],"mask_profile":"mask.scl.v1",'
        '"max_tile_cloud":80.0,"n_min":3,"stats_min_support_fraction":0.5}'
    )


# --- D: engine fingerprints -----------------------------------------------------------------------


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def snapshot_fingerprint(result, tmp_path: Path) -> dict:
    """Bytes-level fingerprint of a snapshot engine result (artifacts written exactly as in production)."""
    from aether.services.snapshot_run import _write_artifacts
    from aether.storage.store import LocalFilesystemStore

    arts = _write_artifacts(result, LocalFilesystemStore(tmp_path / "store"))
    arrays = {}
    for name, c in result.composites.items():
        arrays[f"index:{name}"] = _sha(c.array.data.tobytes()) + ":" + _sha(c.array.valid.tobytes())
        arrays[f"n_valid:{name}"] = _sha(c.n_valid.tobytes())
    if result.display is not None:
        arrays["rgb"] = [_sha(a.data.tobytes()) for a in result.display.arrays]
    stats = {
        n: [s.available, s.unavailable_reason, s.region_pixels, s.n_pixels, s.support_fraction, s.mean, s.median, s.p10, s.p90]
        for n, s in result.stats.items()
    }
    cov = result.coverage
    return {
        "artifacts": {k: a["sha256"] for k, a in sorted(arts.items())},
        "arrays": arrays,
        "steps_sha256": _sha(json.dumps(result.steps, sort_keys=True, separators=(",", ":"), default=str).encode()),
        "step_kinds": [s["kind"] for s in result.steps],
        "stats": stats,
        "coverage": [
            cov.outcome,
            cov.aoi_grid_px_10m,
            cov.aoi_grid_px_20m,
            cov.observed_px,
            cov.valid_px,
            cov.observations_total,
            cov.observations_usable,
            cov.limitations,
        ],
        "observations_used": result.observations_used,
        "mask_reason_px": result.mask_reason_px,
    }


def synthetic_result():
    from aether.geo.aoi import normalize_aoi
    from aether.geo.grid import build_analysis_grid
    from aether.geo.snapshot import run_snapshot
    from aether.geo.stac.eligibility import apply_eligibility, parse_item
    from tests.factories import AOI_GEOJSON, SyntheticReader, stac_item

    items = [stac_item(f"I{i}", datatake=f"DT{i}", dt=f"2024-06-{10 + i:02d}T18:58:21Z") for i in range(4)]
    items.append(stac_item("OLD", datatake="DTX", baseline="04.00", dt="2024-06-20T18:58:21Z"))
    aoi = normalize_aoi(AOI_GEOJSON)
    grid = build_analysis_grid(aoi.geometry, aoi.utm_epsg)
    scenes = [parse_item(i) for i in items]
    apply_eligibility(scenes, grid.epsg)
    reader = SyntheticReader({"I0": 3100, "I1": 4100, "I2": 5100, "I3": 9000}, cloudy={"I3"})
    return run_snapshot(grid=grid, aoi_area_m2=aoi.area_m2, scenes=scenes, reader=reader)


def real_fixture_result():
    from aether.geo.aoi import normalize_aoi
    from aether.geo.grid import build_analysis_grid
    from aether.geo.snapshot import run_snapshot
    from aether.geo.stac.eligibility import apply_eligibility, parse_item
    from tests.unit.test_reference_real_data import ITEMS, TINY, FixtureReader, _aoi_geojson

    items = [json.loads((TINY / "stac" / f"{i}.json").read_text()) for i in ITEMS]
    aoi = normalize_aoi(_aoi_geojson())
    grid = build_analysis_grid(aoi.geometry, aoi.utm_epsg)
    scenes = [parse_item(i) for i in items]
    apply_eligibility(scenes, grid.epsg)
    return run_snapshot(grid=grid, aoi_area_m2=aoi.area_m2, scenes=scenes, reader=FixtureReader())


@pytest.mark.parametrize("scenario", ["synthetic", "real_s2_640m"])
def test_d_snapshot_engine_fingerprint_is_pinned(scenario, tmp_path):
    golden = json.loads(GOLDEN.read_text())["scenarios"][scenario]
    result = synthetic_result() if scenario == "synthetic" else real_fixture_result()
    got = json.loads(json.dumps(snapshot_fingerprint(result, tmp_path), default=str))
    for key in golden:
        assert got[key] == golden[key], f"{scenario}: {key} changed"


# --- version-map separation (Phase 2 freeze C-10) -------------------------------------------------


def test_b_snapshot_map_alias_and_change_map_separation():
    assert science.ALGORITHM_VERSIONS is science.SNAPSHOT_ALGORITHM_VERSIONS  # Phase 1 name kept
    assert list(science.SNAPSHOT_ALGORITHM_VERSIONS.items()) == PHASE1_VERSIONS
    change = science.CHANGE_ALGORITHM_VERSIONS
    assert list(change.items())[: len(PHASE1_VERSIONS)] == PHASE1_VERSIONS  # change = snapshot + Phase 2 entries
    assert {k: v for k, v in change.items() if k not in dict(PHASE1_VERSIONS)} == {
        "change": "change.index_diff.v1",
        "coreg": "coreg.phase_corr.v1",
        "vector": "vector.gpkg.det1",
    }
    assert change is not science.SNAPSHOT_ALGORITHM_VERSIONS
