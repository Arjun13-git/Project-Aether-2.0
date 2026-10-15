"""STAC eligibility (C1 §5–§6), windowed reads (C3 §4–§5) and the snapshot engine end to end."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine

from aether import science
from aether.geo.aoi import normalize_aoi
from aether.geo.grid import build_analysis_grid
from aether.geo.io.windows import (
    CachedAssetReader,
    ItemExcluded,
    RasterioAssetReader,
    RawWindow,
    WindowMeta,
    read_window_from_dataset,
)
from aether.geo.snapshot import run_snapshot
from aether.geo.stac.eligibility import apply_eligibility, exclusion_counts, group_observations, parse_item
from tests.factories import AOI_GEOJSON, PREFIX, SyntheticReader, make_grid, stac_item

# --- eligibility -----------------------------------------------------------------------------


def _scenes(*items):
    return [parse_item(i) for i in items]


def test_valid_item_is_used_and_metadata_captured():
    (s,) = _scenes(stac_item())
    apply_eligibility([s], 32610)
    assert s.status == "used" and s.reason_code is None
    assert s.epsg == 32610 and s.processing_baseline == "05.10" and s.tile_cloud_cover == 5.0
    assert set(s.radiometry) == set(science.REQUIRED_ASSETS)
    assert len(s.stac_sha256) == 64


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"collection": "sentinel-2-l2a"}, "wrong_collection"),
        ({"properties.s2:processing_baseline": "04.00"}, "baseline_unsupported"),
        ({"properties.s2:processing_baseline": None}, "baseline_unsupported"),
        ({"properties.proj:code": "EPSG:32611"}, "different_utm_zone"),
        ({"assets.swir16": None}, "asset_missing:swir16"),
        ({"assets.red.href": "https://evil.example.com/x.tif"}, "href_not_allowed"),
        ({"assets.nir.raster:bands": None}, "radiometry_metadata_missing"),
        (
            {"assets.nir.raster:bands": [{"data_type": "uint16", "scale": 1.0, "offset": 0.0, "nodata": 0}]},
            "radiometry_metadata_unexpected",
        ),
        ({"properties.datetime": None}, "datetime_missing"),
        ({"properties.s2:datatake_id": None}, "datatake_missing"),
    ],
)
def test_exclusion_reasons(overrides, code):
    (s,) = _scenes(stac_item(overrides=overrides))
    apply_eligibility([s], 32610)
    assert (s.status, s.reason_code) == ("excluded", code)


def test_duplicates_keep_highest_baseline_then_latest_generation():
    a = stac_item("A", baseline="05.10", generation_time="2024-06-15T22:00:00Z")
    b = stac_item("B", baseline="05.11", generation_time="2024-06-15T21:00:00Z")
    c = stac_item("C", baseline="05.11", generation_time="2024-06-16T21:00:00Z")
    scenes = _scenes(a, b, c)
    apply_eligibility(scenes, 32610)
    assert [s.status for s in scenes] == ["excluded", "excluded", "used"]
    assert exclusion_counts(scenes) == {"duplicate_superseded": 2}


def test_observation_is_datatake_not_item():
    a = stac_item("A_10SFH", grid_code="MGRS-10SFH", datatake="DT1")
    b = stac_item("B_10SFG", grid_code="MGRS-10SFG", datatake="DT1")
    c = stac_item("C_10SFH", grid_code="MGRS-10SFH", datatake="DT2", dt="2024-06-20T18:58:21Z")
    scenes = _scenes(a, b, c)
    apply_eligibility(scenes, 32610)
    obs = group_observations(scenes)
    assert [o.datatake_id for o in obs] == ["DT1", "DT2"]
    assert [s.item_id for s in obs[0].items] == ["A_10SFH", "B_10SFG"]


# --- windowed reads --------------------------------------------------------------------------


def _write_tif(path: Path, *, epsg=32610, x0=599980.0, y0=4200040.0, res=10, w=20, h=20, dtype="uint16", nodata=0, value=None):
    data = value if value is not None else (np.arange(w * h, dtype=np.uint32).reshape(h, w) + 1).astype(dtype)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=w,
        height=h,
        count=1,
        dtype=dtype,
        crs=f"EPSG:{epsg}",
        transform=Affine(res, 0, x0, 0, -res, y0),
        nodata=nodata,
    ) as dst:
        dst.write(data, 1)
    return data


def test_window_is_exact_integer_offset_with_nodata_fill(tmp_path):
    src = _write_tif(tmp_path / "a.tif")
    g = make_grid("G10", w=6, h=4, x0=600000.0, y0=4200000.0)  # col_off 2, row_off 4
    with rasterio.open(tmp_path / "a.tif") as ds:
        out, meta = read_window_from_dataset(ds, "x", g, 0)
    assert meta.window == (2, 4, 6, 4)
    assert np.array_equal(out, src[4:8, 2:8])
    g2 = make_grid("G10", w=6, h=4, x0=600160.0, y0=4200000.0)  # runs past the right edge (col 18..23)
    with rasterio.open(tmp_path / "a.tif") as ds:
        out2, meta2 = read_window_from_dataset(ds, "x", g2, 0)
    assert np.array_equal(out2[:, :2], src[4:8, 18:20]) and (out2[:, 2:] == 0).all()
    assert meta2.bytes_read_px == 8


def test_window_rejects_other_crs_and_off_lattice(tmp_path):
    _write_tif(tmp_path / "crs.tif", epsg=32611)
    _write_tif(tmp_path / "off.tif", x0=599985.0)
    g = make_grid("G10", w=4, h=4, x0=600000.0, y0=4200000.0)
    for name, code in (("crs.tif", "different_utm_zone"), ("off.tif", "grid_misaligned")):
        with rasterio.open(tmp_path / name) as ds, pytest.raises(ItemExcluded) as e:
            read_window_from_dataset(ds, "x", g, 0)
        assert e.value.reason == code


def test_reader_refuses_non_allowlisted_href():
    with pytest.raises(ItemExcluded) as e:
        RasterioAssetReader().read("/etc/passwd", make_grid(), 0)
    assert e.value.reason == "href_not_allowed"


def test_reader_retries_then_excludes():
    calls, sleeps = [], []

    def opener(href):
        calls.append(href)
        raise rasterio.errors.RasterioIOError("HTTP 503")

    r = RasterioAssetReader(sleep=sleeps.append, opener=opener)
    with pytest.raises(ItemExcluded) as e:
        r.read(PREFIX + "x/RED.tif", make_grid(), 0)
    assert e.value.reason == "asset_unreadable" and len(calls) == 4 and sleeps == [1.0, 2.0, 4.0]


def test_cache_returns_identical_window(tmp_path):
    g = make_grid()

    class Inner:
        n = 0

        def read(self, href, grid, fill, *, cache_key=None):
            self.n += 1
            meta = WindowMeta(href, "uint16", 32610, (10, 0, 0, 0, -10, 0), (0, 0, 8, 6), 1.0, 0.0, 0.0, 48)
            return RawWindow(np.full(grid.shape, 7, np.uint16), meta, False, 0.0)

    inner = Inner()
    c = CachedAssetReader(inner, tmp_path)
    a = c.read(PREFIX + "a.tif", g, 0, cache_key=("gh", "item", "red"))
    b = c.read(PREFIX + "a.tif", g, 0, cache_key=("gh", "item", "red"))
    assert inner.n == 1 and b.from_cache and np.array_equal(a.data, b.data) and a.meta == b.meta


# --- snapshot engine with a synthetic reader --------------------------------------------------


def _engine(items, reader):
    aoi = normalize_aoi(AOI_GEOJSON)
    grid = build_analysis_grid(aoi.geometry, aoi.utm_epsg)
    scenes = [parse_item(i) for i in items]
    apply_eligibility(scenes, grid.epsg)
    return run_snapshot(grid=grid, aoi_area_m2=aoi.area_m2, scenes=scenes, reader=reader), aoi


def _items(n):
    return [stac_item(f"I{i}", datatake=f"DT{i}", dt=f"2024-06-{10 + i:02d}T18:58:21Z") for i in range(n)]


def test_engine_end_to_end_index_first_median():
    items = _items(4)
    nir = {"I0": 3100, "I1": 4100, "I2": 5100, "I3": 9000}
    res, _ = _engine(items, SyntheticReader(nir, cloudy={"I3"}))
    assert res.coverage.outcome == "completed" and res.observations_usable == ["DT0", "DT1", "DT2"]
    red = 1500 * 1e-4 - 0.1
    ndvis = sorted(((v * 1e-4 - 0.1) - red) / ((v * 1e-4 - 0.1) + red) for v in (3100, 4100, 5100))
    c = res.composites["ndvi"]
    centre = c.array.data[c.array.grid.height // 2, c.array.grid.width // 2]
    assert centre == pytest.approx(ndvis[1], abs=1e-6)
    assert res.stats["ndvi"].available and res.stats["ndvi"].median == pytest.approx(ndvis[1], abs=1e-6)
    assert res.coverage.coverage_a == pytest.approx(1.0)
    # cloudy observation contributes masked pixel-observations, reported by reason
    assert res.mask_reason_px["cloud_shadow"] == res.coverage.aoi_grid_px_10m
    assert sum(res.mask_reason_px.values()) == res.aoi_pixel_observations == 4 * res.coverage.aoi_grid_px_10m
    kinds = {s["kind"] for s in res.steps}
    assert {
        "source_read",
        "radiometric_normalization",
        "masking",
        "resampling",
        "index",
        "compositing",
        "statistics",
    } <= kinds
    assert "change_rules" not in kinds and "coregistration" not in kinds


def test_engine_insufficient_observations_never_reports_numbers():
    res, _ = _engine(_items(2), SyntheticReader({"I0": 3000, "I1": 3000}))
    assert res.coverage.outcome == "insufficient_observations"
    assert all(not s.available and s.unavailable_reason == "insufficient_observations" for s in res.stats.values())


def test_engine_read_time_exclusion_is_recorded():
    res, _ = _engine(_items(3), SyntheticReader({"I0": 3000, "I1": 3000, "I2": 3000}, unreadable={"I1"}))
    s1 = next(s for s in res.scenes if s.item_id == "I1")
    assert (s1.status, s1.reason_code) == ("excluded", "asset_unreadable:swir16")
    assert res.coverage.outcome == "insufficient_observations"


def test_engine_with_no_scenes():
    res, _ = _engine([], SyntheticReader({}))
    assert res.coverage.outcome == "insufficient_observations" and res.composites == {} and res.display is None


# --- C1 §8 transient-read policy (offline) ------------------------------------------------------


def _chained_io_error() -> rasterio.errors.RasterioIOError:
    try:
        try:
            raise OSError("CURL error: Operation timed out after 30000 milliseconds")
        except OSError as inner:
            raise rasterio.errors.RasterioIOError("Read failed. See previous exception for details.") from inner
    except rasterio.errors.RasterioIOError as outer:
        return outer


def test_transient_failure_then_success_returns_identical_window(tmp_path):
    src = _write_tif(tmp_path / "a.tif")
    g = make_grid("G10", w=6, h=4, x0=600000.0, y0=4200000.0)
    first = RasterioAssetReader(opener=lambda h: rasterio.open(tmp_path / "a.tif")).read(PREFIX + "x/B04.tif", g, 0)
    calls, sleeps = [], []

    def flaky(href):
        calls.append(href)
        if len(calls) <= 2:
            raise _chained_io_error()
        return rasterio.open(tmp_path / "a.tif")

    again = RasterioAssetReader(sleep=sleeps.append, opener=flaky).read(PREFIX + "x/B04.tif", g, 0)
    assert len(calls) == 3 and sleeps == [1.0, 2.0]
    assert np.array_equal(again.data, first.data) and np.array_equal(again.data, src[4:8, 2:8])
    assert again.meta == first.meta  # a retried read is indistinguishable from a first-time read


def test_exhausted_retries_record_underlying_error_chain():
    def down(href):
        raise _chained_io_error()

    with pytest.raises(ItemExcluded) as e:
        RasterioAssetReader(sleep=lambda s: None, opener=down).read(PREFIX + "x/B03.tif", make_grid(), 0)
    assert e.value.reason == "asset_unreadable" and len(e.value.attempts) == 4
    assert "Operation timed out" in e.value.attempts[0]["detail"]  # the GDAL/curl cause, not only the wrapper


def test_contract_failures_are_not_retried(tmp_path):
    _write_tif(tmp_path / "off.tif", x0=599985.0)
    calls, sleeps = [], []

    def opener(href):
        calls.append(href)
        return rasterio.open(tmp_path / "off.tif")

    with pytest.raises(ItemExcluded) as e:
        RasterioAssetReader(sleep=sleeps.append, opener=opener).read(PREFIX + "x/B04.tif", make_grid(), 0)
    assert e.value.reason == "grid_misaligned" and len(calls) == 1 and sleeps == []


def test_unreadable_item_is_excluded_with_provenance_not_silently_used():
    from aether.evidence.provenance import validate_steps

    class Down(SyntheticReader):
        def read(self, href, grid, fill, *, cache_key=None):
            if cache_key[1] == "I1" and cache_key[2] == "green":
                raise ItemExcluded(
                    "asset_unreadable", href, attempts=[{"attempt": "1", "error": "RasterioIOError", "detail": "HTTP 503"}]
                )
            return super().read(href, grid, fill, cache_key=cache_key)

    res, _ = _engine(_items(4), Down({"I0": 3000, "I1": 3000, "I2": 3000, "I3": 3000}))
    s1 = next(s for s in res.scenes if s.item_id == "I1")
    assert (s1.status, s1.reason_code) == ("excluded", "asset_unreadable:green")
    assert "DT1" not in res.observations_used and len(res.observations_usable) == 3
    failed = [s for s in res.steps if s["kind"] == "source_read_failed"]
    assert len(failed) == 1 and failed[0]["attempts"][0]["detail"] == "HTTP 503"
    assert not any(s["kind"] == "source_read" and s["item_id"] == "I1" for s in res.steps)
    validate_steps(res.steps)
    # the excluded observation is visible in coverage: 4 observations considered, 3 usable
    assert res.coverage.observations_total == 4 and res.coverage.observations_usable == 3
