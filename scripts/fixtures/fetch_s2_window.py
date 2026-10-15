"""Fetch a tiny real Sentinel-2 C1 L2A window as a test fixture (ADR-023 §5).

Usage:
  uv run --project apps/api python scripts/fixtures/fetch_s2_window.py ITEM_ID LON LAT [--size 32] [--assets red,scl]
  uv run --project apps/api python scripts/fixtures/fetch_s2_window.py ITEM_ID LON LAT --size-m 640 --assets all

With ``--size-m`` the window origin is snapped to the 20 m lattice, so 10 m and 20 m assets of every
item of the same UTM zone cover exactly the same square (a real multi-asset analysis grid).

Reads only a window (HTTP range requests) through the C1 href allow-list, keeps the source dtype,
nodata and header scale/offset tags exactly as published, and writes a provenance sidecar plus the
verbatim STAC item JSON. Values are not modified (windowing + recompression only).
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import rasterio
from pyproj import Transformer
from rasterio.windows import Window

from aether import science
from aether.geo.io.windows import GDAL_REMOTE_ENV, check_href

STAC = "https://earth-search.aws.element84.com/v1"
OUT = Path(__file__).resolve().parents[2] / "apps/api/tests/fixtures/tiny"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("item_id")
    ap.add_argument("lon", type=float)
    ap.add_argument("lat", type=float)
    ap.add_argument("--size", type=int, default=32)
    ap.add_argument("--assets", default="red,scl")
    ap.add_argument("--size-m", type=int, default=0, help="square window size in metres, 20 m-lattice snapped")
    args = ap.parse_args()
    if args.assets == "all":
        args.assets = ",".join(science.REQUIRED_ASSETS)
    if args.size_m and (args.size_m % 20 or args.size_m // 10 > 256):
        raise SystemExit("--size-m must be a multiple of 20 and at most 2560 m (256 px at 10 m)")
    if args.size > 256:
        raise SystemExit("ADR-023: at most 256 × 256 px per asset")

    item = httpx.get(f"{STAC}/collections/{science.STAC_COLLECTION}/items/{args.item_id}", timeout=30).raise_for_status().json()
    (OUT / "stac").mkdir(parents=True, exist_ok=True)
    (OUT / "stac" / f"{args.item_id}.json").write_text(json.dumps(item, indent=1, sort_keys=True))

    for key in args.assets.split(","):
        asset = item["assets"][key]
        href = asset["href"]
        check_href(href)
        with rasterio.Env(**GDAL_REMOTE_ENV), rasterio.open(href) as src:
            x, y = Transformer.from_crs(4326, src.crs, always_xy=True).transform(args.lon, args.lat)
            if args.size_m:
                r = src.transform.a
                ox, oy = src.transform.c, src.transform.f
                x_origin = ox + ((x - args.size_m / 2 - ox) // 20) * 20
                y_origin = oy - ((oy - (y + args.size_m / 2)) // 20) * 20
                n = int(args.size_m // r)
                win = Window(int((x_origin - ox) / r), int((oy - y_origin) / r), n, n)
            else:
                row, col = src.index(x, y)
                half = args.size // 2
                win = Window(col - half, row - half, args.size, args.size)
            data = src.read(1, window=win)
            profile = {
                "driver": "GTiff",
                "width": int(win.width),
                "height": int(win.height),
                "count": 1,
                "dtype": src.dtypes[0],
                "crs": src.crs,
                "transform": src.window_transform(win),
                "nodata": src.nodata,
                "compress": "deflate",
                "tiled": False,
            }
            name = f"{args.item_id}_{key}_{args.size_m}m" if args.size_m else f"{args.item_id}_{key}_{args.size}px"
            dst_path = OUT / "sentinel2" / f"{name}.tif"
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            with rasterio.open(dst_path, "w", **profile) as dst:
                dst.write(data, 1)
                dst.scales = src.scales
                dst.offsets = src.offsets
            header = {
                "scales": list(src.scales),
                "offsets": list(src.offsets),
                "nodata": src.nodata,
                "dtype": src.dtypes[0],
                "tags": src.tags(1),
            }
        sidecar = {
            "source": {"provider": "Element 84 Earth Search", "stac_api": STAC, "collection": science.STAC_COLLECTION},
            "item_id": args.item_id,
            "asset_key": key,
            "href": href,
            "file_checksum": asset.get("file:checksum"),
            "processing_baseline": item["properties"].get("s2:processing_baseline"),
            "acquired_at": item["properties"].get("datetime"),
            "mgrs_tile": item["properties"].get("grid:code"),
            "window": {
                "epsg": profile["crs"].to_epsg(),
                "transform": list(profile["transform"])[:6],
                "col_off": int(win.col_off),
                "row_off": int(win.row_off),
                "width": int(win.width),
                "height": int(win.height),
            },
            "source_header": header,
            "stac_raster_bands": asset.get("raster:bands"),
            "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "retrieval_script": "scripts/fixtures/fetch_s2_window.py",
            "licence": "Copernicus Sentinel Data Legal Notice "
            "(https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice)",
            "attribution": f"Contains modified Copernicus Sentinel data {item['properties']['datetime'][:4]}",
            "purpose": "Real COG header vs STAC radiometric metadata cross-check (C2 §3, C1 §7, V-C1-2)",
            "modifications": "windowed and recompressed (DEFLATE); pixel values unchanged",
        }
        dst_path.with_name(f"{name}.provenance.json").write_text(json.dumps(sidecar, indent=1, default=str))
        print(dst_path, dst_path.stat().st_size, "bytes", header)


if __name__ == "__main__":
    main()
