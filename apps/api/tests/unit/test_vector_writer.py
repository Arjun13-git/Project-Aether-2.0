"""Deterministic ``change_events.gpkg`` writer (C5 §10, C-05, C-17; gate G1 cases carried into production)."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pyogrio
import pytest
import rasterio
import shapely
from affine import Affine
from pyogrio import raw
from shapely.geometry import MultiPolygon, Polygon

from aether import science
from aether.domain.errors import InvariantViolation
from aether.geo.change.morphology import label_components
from aether.geo.change.polygons import polygonize_components
from aether.geo.grid import GridArray
from aether.geo.io import vector
from aether.geo.io.vector import FIELDS, LAYER, PINNED_DATE, EventFeature, features_from_events, write_change_events_gpkg
from tests.factories import make_grid

EPSG = 32610
ROOT = Path(__file__).resolve().parents[2]

# Pixel layouts: plain block, diagonal-touch component (MultiPolygon after lossless repair, C-04),
# a ring with a hole, and a block touching the boundary.
LAYOUTS = {
    "veg_loss": ["XXXX", "XXXX", "XXXX"],
    "built_bare_increase": ["X..", ".X.", "..X"],
    "other_significant": ["XXXX", "X..X", "X..X", "XXXX"],
    "water_increase": ["XX", "XX"],
}


def _geom(rows: list[str], x0: float, y0: float):
    m = np.array([[c == "X" for c in r] for r in rows])
    g = make_grid("G10", m.shape[1], m.shape[0], x0=x0, y0=y0)
    labels, _ = label_components(GridArray(g, m, np.ones(m.shape, bool)), g)
    (ev,), _ = polygonize_components(labels, g)
    return ev.geom_utm


def reference_features() -> list[EventFeature]:
    """Fixed events in EPSG:32610, incl. a MultiPolygon, a hole and a cross-category overlap pair."""
    feats = []
    for i, (cat, rows) in enumerate(LAYOUTS.items()):
        feats.append(EventFeature(i + 1, cat, cat == "water_increase", _geom(rows, 600000.0 + 100 * i, 4200000.0)))
    # C-17 overlap: two categories sharing exactly one pixel square.
    a = shapely.box(601000.0, 4199970.0, 601030.0, 4200000.0)
    b = shapely.box(601020.0, 4199950.0, 601050.0, 4199980.0)
    feats.append(EventFeature(5, "veg_gain", False, b))
    feats.append(EventFeature(6, "water_decrease", False, a))
    return feats


def write_reference(path: str) -> str:
    return write_change_events_gpkg(Path(path), reference_features(), epsg=EPSG).sha256


def _bytes(path: Path) -> bytes:
    return path.read_bytes()


# --- G1 case 1 / 4: same process, different paths ------------------------------------------------


def test_same_process_and_different_paths_are_byte_identical(tmp_path):
    (tmp_path / "x" / "deep").mkdir(parents=True)
    p1, p2, p3 = tmp_path / "a.gpkg", tmp_path / "b.gpkg", tmp_path / "x" / "deep" / "renamed.gpkg"
    r1 = write_change_events_gpkg(p1, reference_features(), epsg=EPSG)
    r2 = write_change_events_gpkg(p2, reference_features(), epsg=EPSG)
    r3 = write_change_events_gpkg(p3, reference_features(), epsg=EPSG)
    assert _bytes(p1) == _bytes(p2) == _bytes(p3)
    assert r1.sha256 == r2.sha256 == r3.sha256
    assert str(tmp_path).encode() not in _bytes(p1)  # no path leakage
    # Rewriting the same path replaces the file with identical bytes.
    write_change_events_gpkg(p1, reference_features(), epsg=EPSG)
    assert _bytes(p1) == _bytes(p2)


def test_input_order_does_not_change_bytes(tmp_path):
    feats = reference_features()
    write_change_events_gpkg(tmp_path / "a.gpkg", feats, epsg=EPSG)
    write_change_events_gpkg(tmp_path / "b.gpkg", list(reversed(feats)), epsg=EPSG)
    assert _bytes(tmp_path / "a.gpkg") == _bytes(tmp_path / "b.gpkg")


# --- G1 case 2 / 3: separate processes, different hash seeds, time separation --------------------


def _subprocess_sha(path: Path, seed: str) -> str:
    env = {**os.environ, "PYTHONHASHSEED": seed}
    cmd = [
        sys.executable,
        "-c",
        f"from tests.unit.test_vector_writer import write_reference; print(write_reference({str(path)!r}))",
    ]
    return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, check=True).stdout.strip()


def test_separate_processes_and_time_separation_are_byte_identical(tmp_path):
    local = write_reference(str(tmp_path / "local.gpkg"))
    s0 = _subprocess_sha(tmp_path / "p0.gpkg", "0")
    s1 = _subprocess_sha(tmp_path / "p1.gpkg", "12345")
    time.sleep(2.1)  # gpkg_contents.last_change would differ without the pinned date
    s2 = _subprocess_sha(tmp_path / "p2.gpkg", "4242")
    assert local == s0 == s1 == s2
    assert _bytes(tmp_path / "p0.gpkg") == _bytes(tmp_path / "p2.gpkg")


# --- G1 case 5 / 6: rasterio coexistence and concurrent reads ------------------------------------


def _make_tif(path: Path) -> None:
    data = np.arange(256 * 256, dtype=np.uint16).reshape(256, 256)
    profile = {"driver": "GTiff", "height": 256, "width": 256, "count": 1, "dtype": "uint16", "crs": "EPSG:32610"}
    with rasterio.open(path, "w", transform=Affine(10, 0, 600000, 0, -10, 4200000), **profile) as dst:
        dst.write(data, 1)


def test_deterministic_alongside_concurrent_rasterio_reads(tmp_path):
    tif = tmp_path / "r.tif"
    _make_tif(tif)
    reference = write_reference(str(tmp_path / "ref.gpkg"))

    def read(_):
        with rasterio.open(tif) as src:
            return int(src.read(1, window=((0, 64), (0, 64))).sum())

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(read, i) for i in range(200)]
        shas = [write_reference(str(tmp_path / f"c{i}.gpkg")) for i in range(3)]
        sums = {f.result() for f in futures}
    assert len(sums) == 1  # reads stay consistent
    assert set(shas) == {reference}


# --- read-back, schema, FIDs, geometry exactness, overlap ----------------------------------------


def test_read_back_schema_crs_fids_values_and_exact_geometry(tmp_path):
    feats = reference_features()
    res = write_change_events_gpkg(tmp_path / "e.gpkg", feats, epsg=EPSG)
    meta, fids, wkb, fields = raw.read(str(tmp_path / "e.gpkg"), layer=LAYER, return_fids=True)[:4]
    assert list(meta["fields"]) == [n for n, _ in FIELDS] == ["event_label", "category", "touches_aoi_boundary"]
    assert list(meta["dtypes"]) == ["int32", "object", "bool"]
    assert meta["geometry_type"] == "MultiPolygon" and meta["crs"] == f"EPSG:{EPSG}"
    assert pyogrio.list_layers(str(tmp_path / "e.gpkg")).tolist() == [[LAYER, "MultiPolygon"]]
    ordered = sorted(feats, key=lambda f: f.event_label)
    assert [int(f) for f in fids] == [f.event_label for f in ordered] == list(fields[0])  # FID = event_label
    assert list(fields[1]) == [f.category for f in ordered]
    assert list(fields[2]) == [f.touches_aoi_boundary for f in ordered]
    for f, w in zip(ordered, wkb, strict=True):
        got = shapely.from_wkb(w)
        expected = f.geometry if isinstance(f.geometry, MultiPolygon) else MultiPolygon([f.geometry])
        assert got.equals_exact(expected, tolerance=0)  # coordinates unchanged: no simplify/buffer/normalise
        assert got.area == f.geometry.area and got.is_valid
    assert res.feature_count == len(feats) and res.crs_epsg == EPSG and res.layer == LAYER
    diag = shapely.from_wkb(wkb[1])
    assert len(diag.geoms) == 3  # the C-04 diagonal chain (3 corner-touching pixels) stays one feature, 3 parts
    hole = shapely.from_wkb(wkb[2])
    assert len(hole.geoms[0].interiors) == 1


def test_cross_category_overlap_survives_as_separate_features(tmp_path):
    write_change_events_gpkg(tmp_path / "o.gpkg", reference_features(), epsg=EPSG)
    _, fids, wkb, fields = raw.read(str(tmp_path / "o.gpkg"), layer=LAYER, return_fids=True)[:4]
    by_fid = {int(f): (shapely.from_wkb(w), c) for f, w, c in zip(fids, wkb, fields[1], strict=True)}
    (g5, c5), (g6, c6) = by_fid[5], by_fid[6]
    assert {c5, c6} == {"veg_gain", "water_decrease"}
    assert g5.intersection(g6).area == 100.0  # exactly one shared 10 m pixel square


def test_writes_real_change_events_with_overlap_from_the_engine(tmp_path):
    from tests.unit.test_change_overlap_flags import _extract, fixture_40

    _, res, _ = _extract(fixture_40())
    feats = features_from_events(res.events)
    assert [f.event_label for f in feats] == list(range(1, len(res.events) + 1))
    assert all(f.geometry is e.geometry.geom_utm for f, e in zip(feats, res.events, strict=True))
    out = write_change_events_gpkg(tmp_path / "engine.gpkg", feats, epsg=EPSG)
    _, fids, wkb, _ = raw.read(str(out.path), layer=LAYER, return_fids=True)[:4]
    geoms = [shapely.from_wkb(w) for w in wkb]
    overlaps = [(i, j) for i in range(len(geoms)) for j in range(i + 1, len(geoms)) if geoms[i].intersection(geoms[j]).area > 0]
    assert len(overlaps) == 1 and geoms[overlaps[0][0]].intersection(geoms[overlaps[0][1]]).area == 100.0
    assert [int(f) for f in fids] == [e.event_label for e in res.events]


# --- pinned metadata, result fields, configuration scope ------------------------------------------


def test_pinned_date_and_result_metadata(tmp_path):
    before = pyogrio.get_gdal_config_option("OGR_CURRENT_DATE")
    res = write_change_events_gpkg(tmp_path / "m.gpkg", reference_features(), epsg=EPSG)
    assert pyogrio.get_gdal_config_option("OGR_CURRENT_DATE") == before  # config scoped to the write
    with sqlite3.connect(tmp_path / "m.gpkg") as con:
        rows = con.execute("SELECT table_name, last_change, srs_id FROM gpkg_contents").fetchall()
        rtree = con.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'rtree_change_events_geom%'").fetchone()[0]
    assert rows == [(LAYER, PINNED_DATE, EPSG)]
    assert rtree > 0  # SPATIAL_INDEX=YES
    data = (tmp_path / "m.gpkg").read_bytes()
    assert res.bytes == len(data) and res.sha256 == hashlib.sha256(data).hexdigest()
    assert res.media_type == "application/geopackage+sqlite3" and res.writer_id == science.VECTOR_WRITER_ID == "vector.gpkg.det1"
    assert res.pyogrio_version == "0.13.0" and res.gdal_version == pyogrio.__gdal_version_string__
    assert res.sqlite_version_number == int.from_bytes(data[96:100], "big") > 3_000_000


def test_empty_event_set_writes_a_deterministic_empty_layer(tmp_path):
    a = write_change_events_gpkg(tmp_path / "a.gpkg", [], epsg=EPSG)
    b = write_change_events_gpkg(tmp_path / "b.gpkg", [], epsg=EPSG)
    assert a.sha256 == b.sha256 and a.feature_count == 0
    meta, fids = raw.read(str(a.path), layer=LAYER, return_fids=True)[:2]
    assert len(fids) == 0 and meta["geometry_type"] == "MultiPolygon" and list(meta["fields"]) == [n for n, _ in FIELDS]


# --- input validation (fail loudly; never repair or reproject) -----------------------------------


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda fs: (fs, 4326), "vector_crs_not_utm"),
        (lambda fs: ([*fs[:-1], EventFeature(9, "veg_gain", False, fs[-1].geometry)], EPSG), "vector_event_labels"),
        (lambda fs: ([*fs, EventFeature(len(fs), "veg_gain", False, fs[0].geometry)], EPSG), "vector_event_labels"),
        (lambda fs: ([*fs[:-1], EventFeature(len(fs), "forest", False, fs[-1].geometry)], EPSG), "vector_category"),
        (
            lambda fs: ([*fs[:-1], EventFeature(len(fs), "veg_gain", False, Polygon([(0, 0), (1, 1), (1, 0), (0, 1)]))], EPSG),
            "vector_geometry",
        ),
        (lambda fs: ([*fs[:-1], EventFeature(len(fs), "veg_gain", False, shapely.Point(0, 0))], EPSG), "vector_geometry"),
    ],
)
def test_invalid_inputs_fail_loudly(tmp_path, mutate, code):
    feats, epsg = mutate(reference_features())
    with pytest.raises(InvariantViolation, match=code):
        write_change_events_gpkg(tmp_path / "bad.gpkg", feats, epsg=epsg)
    assert not (tmp_path / "bad.gpkg").exists()


def test_writer_constants_match_the_g1_settings():
    assert vector.LAYER_OPTIONS == {"SPATIAL_INDEX": "YES", "FID": "fid", "GEOMETRY_NAME": "geom"}
    assert vector.PINNED_DATE == "2000-01-01T00:00:00.000Z" and vector.DRIVER == "GPKG"
    assert vector.GEOMETRY_TYPE == "MultiPolygon"
