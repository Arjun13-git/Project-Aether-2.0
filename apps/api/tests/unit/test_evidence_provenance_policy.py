"""Evidence builders (C7), provenance schemas, settings guards, reproducibility (I-12) and the
fixture policy (ADR-023)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from aether import science
from aether.domain.errors import InvariantViolation
from aether.evidence.builders import EvidenceDraft, MeasurementDraft, content_sha256, validate_evidence
from aether.evidence.provenance import environment, validate_steps
from aether.geo.io.cog import write_cog
from aether.settings import Settings
from aether.storage.store import LocalFilesystemStore, sha256_file
from tests.factories import make_grid

M = "coverage.v1@1"


def ev(measurements, facts=None, limitations=None):
    return EvidenceDraft("coverage_statistic", facts or {"outcome": "completed"}, measurements, limitations or [])


def test_valid_evidence_passes():
    validate_evidence(
        ev(
            [
                MeasurementDraft("aoi_grid_area", "value", 1000.0, M),
                MeasurementDraft("coverage_a", "fraction", 0.9, M, denominator="aoi_grid_area"),
            ]
        )
    )


@pytest.mark.parametrize(
    ("draft", "code"),
    [
        (ev([MeasurementDraft("coverage_a", "fraction", 0.9, M)]), "fraction_denominator"),
        (ev([MeasurementDraft("coverage_a", "fraction", 0.9, M, denominator="aoi_area")]), "fraction_denominator"),
        (ev([MeasurementDraft("coverage_a", "fraction", 0.9, M, denominator="aoi_grid_area")]), "fraction_denominator"),
        (ev([MeasurementDraft("changed_fraction", "fraction", 0.1, M)]), "unknown_quantity"),
        (ev([MeasurementDraft("aoi_area", "value", float("nan"), M)]), "measurement_not_finite"),
        (ev([MeasurementDraft("aoi_area", "value", None, M)]), "unavailable_without_reason"),
        (ev([MeasurementDraft("aoi_area", "mean", 1.0, M)]), "statistic_not_allowed"),
        (ev([MeasurementDraft("index.evi", "mean", 0.1, M)]), "unknown_quantity"),
        (ev([], facts={"coverage": 0.72}), "facts_contain_number"),
        (ev([], facts={"nested": [{"n": 3}]}), "facts_contain_number"),
        (ev([], limitations=["calibrated_thresholds"]), "unknown_limitation"),
    ],
)
def test_invalid_evidence_rejected(draft, code):
    with pytest.raises(InvariantViolation) as e:
        validate_evidence(draft)
    assert e.value.code == code


def test_numeric_identifier_facts_allowed_and_hash_is_stable():
    d = ev([MeasurementDraft("aoi_area", "value", 5.0, M, measurement_id="ms_" + "1" * 32)], facts={"epsg": 32610})
    validate_evidence(d)
    assert content_sha256(d, "run") == content_sha256(d, "run") != content_sha256(d, "other")


def test_provenance_step_schema():
    validate_steps(
        [
            {
                "kind": "compositing",
                "version": "v",
                "index": "ndvi",
                "statistic": "median",
                "strategy": "per_observation_index_then_temporal_median",
                "n_min": 3,
                "observation_ids": [],
            }
        ]
    )
    with pytest.raises(InvariantViolation):
        validate_steps(
            [
                {
                    "kind": "compositing",
                    "version": "v",
                    "index": "ndvi",
                    "statistic": "median",
                    "strategy": "band_median_then_index",
                    "n_min": 3,
                    "observation_ids": [],
                }
            ]
        )
    with pytest.raises(InvariantViolation):
        validate_steps([{"kind": "teleport"}])


def test_environment_is_allow_listed():
    env = environment()
    assert set(env) <= {"python", "os", "device", "numpy", "rasterio", "pyproj", "shapely", "scipy", "gdal"}


def test_settings_guards():
    with pytest.raises(ValueError):
        Settings(bind_host="0.0.0.0")
    with pytest.raises(ValueError):
        Settings(stac_collection="sentinel-2-l2a")
    with pytest.raises(ValueError):
        Settings(stac_api_url="http://insecure.example")
    assert Settings(bind_host="localhost").bind_host == "localhost"


def test_cog_and_store_are_deterministic(tmp_path):
    """I-12: identical arrays → identical COG bytes → identical content address."""
    g = make_grid("G10", w=40, h=30)
    rng = np.random.default_rng(1)
    arr = rng.normal(0.3, 0.1, g.shape).astype(np.float32)
    arr[0, :5] = np.nan
    store = LocalFilesystemStore(tmp_path / "s")
    shas = []
    for _ in range(2):
        p = store.tmp_path(".tif")
        meta = write_cog(p, g, [arr], dtype="float32", nodata=float("nan"), band_names=["ndvi"], categorical=False)
        shas.append(store.put_file(p, "tif").sha256)
    assert shas[0] == shas[1] and meta["transform"] == list(tuple(g.transform)[:6])
    path = Path(store.open_read_path(shas[0], "tif"))
    assert sha256_file(path) == shas[0]
    with pytest.raises(ValueError):
        store.open_read_path("../../etc/passwd", "tif")


def test_versions_are_the_contract_versions():
    assert science.ALGORITHM_VERSIONS == {
        "ingest": "ingest.earth_search.v1",
        "radiometry": "radiometry.s2c1.v1",
        "grid": "grid.utm_s2.v1",
        "mask": "mask.scl.v1",
        "composite": "composite.median_index.v1",
        "indices": "indices.s2.v1",
        "coverage": "coverage.v1",
        "zonal_stats": "zonal_stats@1",
    }
    assert set(science.INDICES) == {"ndvi", "ndwi", "mndwi", "ndbi"}  # frozen MVP set (C4 §4)
    assert {p["calibration"] for p in science.parameter_calibration_table()} == {"untuned"}


# --- ADR-023 fixture policy --------------------------------------------------------------------

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REPO = Path(__file__).resolve().parents[4]


def test_fixture_tree_respects_adr023():
    files = [p for p in FIXTURES.rglob("*") if p.is_file()]
    assert sum(p.stat().st_size for p in files) <= 20 * 1024 * 1024
    for p in files:
        assert p.stat().st_size <= 512 * 1024, f"{p} exceeds 512 KiB"
        if p.suffix.lower() in (".tif", ".tiff"):
            assert p.suffix == ".tif" and "tiny" in p.relative_to(FIXTURES).parts, f"{p} outside fixtures/tiny"
            assert p.stat().st_size <= 256 * 1024
            if "sentinel2" in p.parts:
                assert p.with_name(p.stem + ".provenance.json").exists(), f"{p} has no provenance sidecar"
    assert (FIXTURES / "README.md").exists()


def test_no_rasters_outside_fixture_tree():
    stray = [
        p
        for p in REPO.rglob("*.tif*")
        if ".git" not in p.parts
        and ".data" not in p.parts
        and "node_modules" not in p.parts
        and ".venv" not in p.parts
        and FIXTURES not in p.parents
    ]
    assert stray == []
