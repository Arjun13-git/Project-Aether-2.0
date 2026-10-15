"""Radiometry (C2), SCL mask (C4 §2) and resampling R1/R2 (C3 §6)."""

from __future__ import annotations

import numpy as np
import pytest

from aether import science
from aether.geo import masks, resample
from aether.geo.radiometry import AssetRadiometry, check_header, normalize, validate_stac_metadata
from tests.factories import REFL_BANDS, SCL_BANDS, garr, make_grid

META = AssetRadiometry("red", "uint16", 0.0001, -0.1, 0)


def test_fixture_values_from_contract():
    dn = np.array([[0, 500, 1000, 3000, 65535]], dtype=np.uint16)
    n = normalize(dn, META)
    assert n.reflectance.dtype == np.float32
    assert np.isnan(n.reflectance[0, 0]) and not n.valid[0, 0]
    assert n.reflectance[0, 1] == pytest.approx(-0.05, abs=1e-7)  # kept, not clipped
    assert n.reflectance[0, 2] == pytest.approx(0.0, abs=1e-7)
    assert n.reflectance[0, 3] == pytest.approx(0.2, abs=1e-7)
    assert np.isnan(n.reflectance[0, 4]) and not n.valid[0, 4]
    assert (n.n_nodata, n.n_saturated) == (1, 1)


def test_valid_metadata_accepted():
    m = validate_stac_metadata("red", {"raster:bands": REFL_BANDS})
    assert isinstance(m, AssetRadiometry) and (m.scale, m.offset, m.nodata) == (0.0001, -0.1, 0)
    assert isinstance(validate_stac_metadata("scl", {"raster:bands": SCL_BANDS}), AssetRadiometry)


@pytest.mark.parametrize(
    ("bands", "code"),
    [
        (None, "radiometry_metadata_missing"),
        ([], "radiometry_metadata_missing"),
        ([{"data_type": "uint16", "nodata": 0, "offset": -0.1}], "radiometry_metadata_missing"),
        ([{"data_type": "uint16", "nodata": 0, "scale": 0.0001}], "radiometry_metadata_missing"),
        ([{"data_type": "uint16", "scale": 0.0001, "offset": 0.0, "nodata": 0}], "radiometry_metadata_unexpected"),
        ([{"data_type": "uint16", "scale": 0.0002, "offset": -0.1, "nodata": 0}], "radiometry_metadata_unexpected"),
        ([{"data_type": "int16", "scale": 0.0001, "offset": -0.1, "nodata": 0}], "radiometry_metadata_unexpected"),
        ([{"data_type": "uint16", "scale": 0.0001, "offset": -0.1, "nodata": 65535}], "radiometry_metadata_unexpected"),
    ],
)
def test_invalid_metadata_excludes_with_code(bands, code):
    assert validate_stac_metadata("nir", {"raster:bands": bands}) == code


def test_header_cross_check():
    assert check_header(META, "uint16", 1.0, 0.0, None) is None  # GDAL default = tags absent
    assert check_header(META, "uint16", 0.0001, -0.1, 0.0) is None
    assert check_header(META, "int16", 1.0, 0.0, None) == "radiometry_dtype_mismatch"
    assert check_header(META, "uint16", 0.0002, -0.1, None) == "radiometry_metadata_inconsistent"
    assert check_header(META, "uint16", 1.0, 0.0, 65535.0) == "radiometry_metadata_inconsistent"


def test_scl_classes_and_reasons():
    g20 = make_grid("G20", w=12, h=1)
    codes = np.array([[0, 1, 2, 4, 5, 6, 7, 11, 4, 4, 4, 4]], dtype=np.uint8)
    m = masks.scl_mask(garr(g20, codes))
    v = m.valid.data[0]
    assert list(v) == [False, False, False, True, True, True, False, False, True, True, True, True]
    assert m.reason[0, 0] == masks.NODATA
    assert m.reason[0, 2] == masks.CLOUD_SHADOW  # cast shadow
    assert m.reason[0, 6] == masks.OTHER_INVALID  # unclassified
    assert m.reason[0, 7] == masks.SNOW_ICE


@pytest.mark.parametrize("cloud_code", [3, 8, 9, 10])
def test_cloud_and_shadow_masked_and_dilated_by_disk_r3(cloud_code):
    g20 = make_grid("G20", w=11, h=11)
    codes = np.full((11, 11), 4, np.uint8)
    codes[5, 5] = cloud_code
    m = masks.scl_mask(garr(g20, codes))
    yy, xx = np.mgrid[0:11, 0:11]
    inside = (yy - 5) ** 2 + (xx - 5) ** 2 <= 9
    assert np.array_equal(~m.valid.data, inside)  # exactly the radius-3 disk (60 m)
    assert m.dilated_px == inside.sum() - 1
    assert np.all(m.reason[inside] == masks.CLOUD_SHADOW)


def test_dilation_is_deterministic():
    rng = np.random.default_rng(0)
    g20 = make_grid("G20", w=40, h=30)
    codes = rng.choice([4, 5, 6, 8, 9], size=(30, 40), p=[0.4, 0.3, 0.2, 0.05, 0.05]).astype(np.uint8)
    a, b = masks.scl_mask(garr(g20, codes)), masks.scl_mask(garr(g20, codes.copy()))
    assert np.array_equal(a.valid.data, b.valid.data)


def test_disk_shape():
    d = masks.disk(3)
    assert d.shape == (7, 7) and d.sum() == 29 and not d[0, 0] and d[0, 3]


def test_r1_replication_and_r2_block_mean():
    g10, g20 = make_grid("G10", w=4, h=2), make_grid("G20", w=2, h=1)
    up, step = resample.r1_nearest_20_to_10(garr(g20, np.array([[1, 2]], np.uint8)), g10)
    assert up.data.tolist() == [[1, 1, 2, 2], [1, 1, 2, 2]] and step["rule"] == "R1"
    vals = np.array([[0.1, 0.3, 0.2, np.nan], [0.1, 0.3, 0.2, 0.2]], np.float32)
    down, step = resample.r2_block_mean_10_to_20(garr(g10, vals), g20)
    assert down.data[0, 0] == pytest.approx(0.2) and down.valid[0, 0]
    assert np.isnan(down.data[0, 1]) and not down.valid[0, 1]  # one invalid child → invalid
    assert step["rule"] == "R2"


def test_resampling_requires_exact_nesting():
    from aether.domain.errors import InvariantViolation

    g10, g20 = make_grid("G10", w=4, h=2), make_grid("G20", w=3, h=1)
    with pytest.raises(InvariantViolation):
        resample.r2_block_mean_10_to_20(garr(g10, np.zeros((2, 4), np.float32)), g20)


def test_mask_profile_constants_are_the_contract():
    assert science.SCL_MASK.version == "mask.scl.v1"
    assert science.SCL_MASK.valid_classes == {4, 5, 6}
    assert science.SCL_MASK.dilated_classes == {3, 8, 9, 10}
    assert science.SCL_MASK.dilation_radius_px_20m == 3
