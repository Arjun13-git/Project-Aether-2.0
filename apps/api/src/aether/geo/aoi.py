"""AOI admissibility and normalisation (C3 §2, C7 §3).

The AOI is user input in EPSG:4326 (RFC 7946 lon/lat). Admissibility rules reject with stable
codes. The only geometry repair allowed is ``make_valid`` when it changes the geodesic area by
< 0.1 %; the repair is recorded in ``operations`` so the user's geometry is never silently changed.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any

import shapely
from pyproj import Geod
from shapely.geometry import MultiPolygon, Polygon, box, mapping, shape
from shapely.geometry.polygon import orient

from aether import science
from aether.domain.errors import AetherError

_GEOD = Geod(ellps="WGS84")


@dataclass(frozen=True)
class NormalizedAOI:
    geometry: MultiPolygon  # EPSG:4326, oriented, rounded to 1e-9°
    geometry_hash: str
    utm_zone: int
    utm_epsg: int
    area_m2: float  # geodesic (WGS84)
    operations: list[str] = field(default_factory=list)

    def geojson(self) -> dict[str, Any]:
        return dict(mapping(self.geometry))


def geodesic_area_m2(geom: Polygon | MultiPolygon) -> float:
    area, _ = _GEOD.geometry_area_perimeter(geom)
    return abs(area)


def utm_zone_for_lon(lon: float) -> int:
    """Standard UTM zone; lon = 180 belongs to zone 60."""
    return min(math.floor((lon + 180.0) / 6.0) + 1, 60)


def _reject(code: str, message: str, **details: Any) -> AetherError:
    return AetherError(code, message, http_status=422, details=details)


def _vertex_count(geom: MultiPolygon) -> int:
    n = 0
    for poly in geom.geoms:
        n += len(poly.exterior.coords) - 1
        n += sum(len(r.coords) - 1 for r in poly.interiors)
    return n


def _as_multipolygon(geom: Any) -> MultiPolygon:
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    if isinstance(geom, MultiPolygon):
        return geom
    polys = [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon) and not g.is_empty]
    for g in getattr(geom, "geoms", []):
        if isinstance(g, MultiPolygon):
            polys.extend(g.geoms)
    if not polys:
        raise _reject("invalid_geometry", "geometry has no polygonal area")
    return MultiPolygon(polys)


def _round(geom: MultiPolygon) -> MultiPolygon:
    rounded = shapely.set_precision(geom, 10.0**-science.AOI_COORD_ROUND_DEG)
    return _as_multipolygon(rounded)


def _orient(geom: MultiPolygon) -> MultiPolygon:
    return MultiPolygon([orient(p, sign=1.0) for p in geom.geoms])


def geometry_hash(geom: MultiPolygon) -> str:
    return hashlib.sha256(shapely.to_wkb(geom, hex=False, output_dimension=2)).hexdigest()


def normalize_aoi(geojson: dict[str, Any]) -> NormalizedAOI:
    """Validate a GeoJSON Polygon/MultiPolygon against C3 §2 and normalise it."""
    if not isinstance(geojson, dict) or geojson.get("type") not in ("Polygon", "MultiPolygon"):
        raise _reject("invalid_geometry", "AOI must be a GeoJSON Polygon or MultiPolygon")
    if "crs" in geojson:
        raise _reject("invalid_geometry", "legacy GeoJSON 'crs' member is not accepted (RFC 7946)")
    try:
        raw = shape(geojson)
    except Exception as exc:  # malformed coordinates
        raise _reject("invalid_geometry", f"cannot parse geometry: {exc}") from exc
    if raw.is_empty:
        raise _reject("invalid_geometry", "geometry is empty")
    if raw.has_z:
        raw = shapely.force_2d(raw)

    minx, miny, maxx, maxy = raw.bounds
    if minx < -180 or maxx > 180 or miny < -90 or maxy > 90:
        raise _reject("invalid_geometry", "coordinates must be lon/lat within [-180,180]×[-90,90]")

    operations: list[str] = []
    geom = _as_multipolygon(raw) if raw.is_valid else None
    if geom is None:
        repaired = _as_multipolygon(shapely.make_valid(raw))
        # Area of the rings as drawn (signed shoelace per ring): a bow-tie cancels to ~0 and is
        # rejected; a sliver self-touch keeps its area and is repaired.
        before = geodesic_area_m2(raw)
        after = geodesic_area_m2(repaired)
        if before <= 0 or abs(after - before) / before >= science.AOI_REPAIR_MAX_AREA_CHANGE:
            raise _reject(
                "invalid_geometry",
                "geometry is invalid and repair would change its area by ≥ 0.1 %",
                reason=shapely.is_valid_reason(raw),
            )
        geom = repaired
        operations.append(f"make_valid ({shapely.is_valid_reason(raw)})")

    geom = _orient(_round(geom))
    if not geom.is_valid:
        raise _reject("invalid_geometry", "geometry is invalid after coordinate rounding")
    operations.append(f"orient_ccw+round_1e-{science.AOI_COORD_ROUND_DEG}")

    if _vertex_count(geom) > science.AOI_MAX_VERTICES:
        raise _reject("too_many_vertices", f"AOI has more than {science.AOI_MAX_VERTICES} vertices")

    area = geodesic_area_m2(geom)
    if area > science.AOI_MAX_AREA_M2:
        raise _reject("aoi_too_large", "AOI geodesic area exceeds 100 km²", area_m2=round(area))

    minx, miny, maxx, maxy = geom.bounds
    if miny < -science.AOI_MAX_ABS_LAT or maxy > science.AOI_MAX_ABS_LAT:
        raise _reject("latitude_out_of_range", "every latitude must be within ±80°")
    if maxx - minx > 180:
        raise _reject("crosses_antimeridian", "AOI crosses the antimeridian")

    zones = {utm_zone_for_lon(x) for poly in geom.geoms for ring in [poly.exterior, *poly.interiors] for x, _ in ring.coords}
    if len(zones) != 1:
        raise _reject("aoi_crosses_utm_zone", "AOI must lie within one UTM zone", zones=sorted(zones))
    if miny < 0 < maxy:
        raise _reject("crosses_equator", "AOI crosses the equator")
    for name, bounds in science.UTM_EXCEPTION_REGIONS:
        if geom.intersects(box(*bounds)):
            raise _reject("utm_exception_region", f"AOI intersects the {name} UTM exception region")

    zone = zones.pop()
    north = geom.centroid.y >= 0
    epsg = (32600 if north else 32700) + zone
    return NormalizedAOI(geom, geometry_hash(geom), zone, epsg, area, operations)
