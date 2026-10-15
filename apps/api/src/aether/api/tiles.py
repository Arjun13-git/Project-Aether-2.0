"""Map raster delivery by artifact ID with fixed rendering (ADR-021).

Implemented directly on rio-tiler (TiTiler's tiling engine) behind one artifact-ID route instead
of mounting TiTiler's TilerFactory: the factory's public query parameters (``url``, ``rescale``,
``colormap``, ``expression``) would bypass the fixed per-product rendering and the "no arbitrary
URL" rule. Display reprojection to EPSG:3857 happens here only (display, never measurement).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException, Response
from rio_tiler.colormap import cmap
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io import Reader
from rio_tiler.utils import render
from sqlalchemy import text

from aether import science
from aether.db.engine import get_engine
from aether.settings import get_settings
from aether.storage.store import LocalFilesystemStore

router = APIRouter(prefix="/tiles", tags=["tiles"])


@dataclass(frozen=True)
class Rendering:
    name: str
    kinds: tuple[str, ...]
    rescale: tuple[float, float] | None
    colormap: str | None
    categorical: bool
    legend: dict[str, Any]


INDEX_CMAPS = {"ndvi": "brbg", "ndwi": "rdbu", "mndwi": "rdbu", "ndbi": "puor"}  # colour-blind-safe diverging
N_VALID_MAX = 30

RENDERINGS: dict[str, Rendering] = {
    "rgb": Rendering(
        "rgb",
        ("composite",),
        science.DISPLAY_RGB_STRETCH,
        None,
        False,
        {
            "type": "rgb",
            "unit": "BOA reflectance",
            "range": list(science.DISPLAY_RGB_STRETCH),
            "stretch": "linear",
            "note": "display composite (per-band median); not a measurement",
        },
    ),
    **{
        f"index:{name}": Rendering(
            f"index:{name}",
            ("index",),
            (-1.0, 1.0),
            cm,
            False,
            {"type": "diverging", "colormap": cm, "unit": "index (dimensionless)", "range": [-1, 1], "index": name},
        )
        for name, cm in INDEX_CMAPS.items()
    },
    "n_valid": Rendering(
        "n_valid",
        ("n_valid",),
        (0.0, float(N_VALID_MAX)),
        "viridis",
        True,
        {"type": "sequential", "colormap": "viridis", "unit": "observations", "range": [0, N_VALID_MAX]},
    ),
    "insufficient": Rendering(
        "insufficient",
        ("n_valid",),
        None,
        None,
        True,
        {"type": "mask", "colour": "#7f7f7f", "meaning": f"no reliable data: n_valid < n_min ({science.N_MIN.value})"},
    ),
}


def renderings_for(kind: str, band_names: list[str]) -> list[Rendering]:
    if kind == "index":
        return [RENDERINGS[f"index:{band_names[0]}"]]
    return [r for r in RENDERINGS.values() if kind in r.kinds and not r.name.startswith("index:")]


def _artifact(artifact_id: uuid.UUID) -> dict[str, Any]:
    with get_engine().connect() as conn:
        row = (
            conn.execute(text("SELECT id, sha256, kind, band_names FROM artifacts WHERE id=:id"), {"id": artifact_id})
            .mappings()
            .first()
        )
    if row is None:
        raise HTTPException(404, "artifact not found")
    return dict(row)


@router.get("/{artifact_id}/{z}/{x}/{y}.png", responses={200: {"content": {"image/png": {}}}})
def tile(artifact_id: uuid.UUID, z: int, x: int, y: int, render_name: str = "") -> Response:
    art = _artifact(artifact_id)
    allowed = renderings_for(art["kind"], art["band_names"] or [])
    if not allowed:
        raise HTTPException(404, "artifact is not a map layer")
    r = next((a for a in allowed if a.name == render_name), allowed[0]) if render_name else allowed[0]
    if render_name and r.name != render_name:
        raise HTTPException(422, f"rendering {render_name!r} is not defined for this artifact")
    store = LocalFilesystemStore(get_settings().storage_root)
    path = store.open_read_path(art["sha256"], "tif")
    try:
        with Reader(path) as src:
            img = src.tile(x, y, z, tilesize=256, resampling_method="nearest" if r.categorical else "bilinear")
    except TileOutsideBounds:
        return Response(status_code=204)

    if r.name == "insufficient":
        data = img.array.data[0]
        inside = ~np.ma.getmaskarray(img.array)[0] & img.mask.astype(bool)
        show = inside & (data < science.N_MIN.value)
        rgba = np.zeros((4, *data.shape), np.uint8)
        rgba[:3] = 127
        rgba[3] = np.where(show, 150, 0)
        return Response(render(rgba[:3], mask=rgba[3], img_format="PNG"), media_type="image/png")
    if r.rescale is not None:
        img.rescale(in_range=[r.rescale] * img.count)
    content = img.render(img_format="PNG", colormap=cmap.get(r.colormap) if r.colormap else None)
    return Response(content, media_type="image/png", headers={"Cache-Control": "public, max-age=86400, immutable"})
