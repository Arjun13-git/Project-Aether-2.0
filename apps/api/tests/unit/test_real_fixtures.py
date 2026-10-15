"""Real Sentinel-2 C1 L2A fixtures (ADR-023): verbatim STAC item + 32 px windows.

Checks the contracts against real published metadata, not only synthetic assumptions.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import rasterio

from aether.geo.grid import Grid
from aether.geo.io.windows import read_window_from_dataset
from aether.geo.radiometry import check_header, normalize
from aether.geo.stac.eligibility import apply_eligibility, parse_item

TINY = Path(__file__).resolve().parents[1] / "fixtures" / "tiny"
ITEM = "S2A_T10SFH_20240602T185554_L2A"


def _item() -> dict:
    return json.loads((TINY / "stac" / f"{ITEM}.json").read_text())


def test_real_item_is_eligible_with_verified_radiometry():
    scene = parse_item(_item())
    apply_eligibility([scene], 32610)
    assert scene.status == "used", scene.reason_code
    assert scene.processing_baseline == "05.10" and scene.datatake_id
    assert scene.radiometry["red"].scale == 0.0001 and scene.radiometry["red"].offset == -0.1
    assert scene.radiometry["scl"].scale is None
    assert set(scene.assets) == {"blue", "green", "red", "nir", "swir16", "scl"}  # nothing else is read


def _grid_for(src: rasterio.io.DatasetReader) -> Grid:
    t = src.transform
    return Grid("G10" if t.a == 10 else "G20", src.crs.to_epsg(), int(t.a), t.c, t.f, src.width, src.height, "fixture")


def test_real_cog_headers_match_stac_and_normalise():
    scene = parse_item(_item())
    apply_eligibility([scene], 32610)
    for key in ("red", "swir16", "scl"):
        path = TINY / "sentinel2" / f"{ITEM}_{key}_32px.tif"
        with rasterio.open(path) as src:
            data, meta = read_window_from_dataset(src, str(path), _grid_for(src), 0)
        rm = scene.radiometry[key]
        # V-C1-2: headers carry scale/offset/nodata equal to STAC → accepted, never "inconsistent".
        assert check_header(rm, meta.dtype, meta.header_scale, meta.header_offset, meta.header_nodata) is None
        if key == "scl":
            assert data.dtype == np.uint8 and set(np.unique(data)) <= set(range(12))
        else:
            n = normalize(data, rm)
            vals = n.reflectance[n.valid]
            assert vals.size > 0 and np.all(vals > -0.1) and np.all(vals < 2.0)
