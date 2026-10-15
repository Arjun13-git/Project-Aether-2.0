"""Deterministic GeoPackage writer for change events: the authoritative ``change_events.gpkg``.

Contracts: C5 §10 (authoritative format, Phase 2 freeze C-05), C5 §7 step 4/4a/4b (pixel-edge UTM
geometry, C-04 validity, C-17 overlap and global ``event_label``), implementation plan §5 (schema),
and the settings proven byte-deterministic by gate G1 (pyogrio 0.13.0, bundled GDAL 3.12.4):

- layer ``change_events``, driver ``GPKG``, one ``MultiPolygon`` layer in the analysis **UTM** CRS;
- fixed ordered schema ``event_label`` (int32), ``category`` (text), ``touches_aoi_boundary`` (bool);
- features sorted by global ``event_label``; FID = ``event_label`` (labels must be exactly 1..N);
- layer options ``SPATIAL_INDEX=YES``, ``FID=fid``, ``GEOMETRY_NAME=geom``; encoding UTF-8;
- GDAL ``OGR_CURRENT_DATE`` pinned for the write (the only varying field G1 observed);
- a fresh file per write; 2-D little-endian WKB from shapely; no post-write byte edits.

Geometry is written exactly as polygonisation produced it. A Polygon is promoted to a one-part
MultiPolygon (lossless, so the layer type is uniform). Nothing is simplified, buffered, clipped,
re-validated, normalised or reprojected; overlapping events of different categories stay separate
features (C-17). Byte identity holds for the same writer build (I-12), so the build versions are
returned for provenance.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np
import pyogrio
import shapely
from pyogrio import raw
from shapely.geometry import MultiPolygon, Polygon

from aether import science
from aether.domain.errors import InvariantViolation
from aether.geo.change.events import ChangeEvent
from aether.geo.change.rules import CATEGORIES

LAYER: Final = "change_events"
DRIVER: Final = "GPKG"
MEDIA_TYPE: Final = "application/geopackage+sqlite3"
GEOMETRY_TYPE: Final = "MultiPolygon"
FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("event_label", "int32"),
    ("category", "object"),
    ("touches_aoi_boundary", "bool"),
)
LAYER_OPTIONS: Final[dict[str, str]] = {"SPATIAL_INDEX": "YES", "FID": "fid", "GEOMETRY_NAME": "geom"}
PINNED_DATE: Final = "2000-01-01T00:00:00.000Z"
_GDAL_CONFIG_KEY: Final = "OGR_CURRENT_DATE"
_WRITE_LOCK = threading.Lock()  # the GDAL config option is process-wide; scope it to one write at a time


@dataclass(frozen=True)
class EventFeature:
    """One change event as written to the GeoPackage (C-17 global label; geometry in UTM)."""

    event_label: int
    category: str
    touches_aoi_boundary: bool
    geometry: Polygon | MultiPolygon


@dataclass(frozen=True)
class VectorArtifact:
    """What later persistence needs to register the file as a ``vector_gpkg`` artifact."""

    path: Path
    sha256: str
    bytes: int
    crs_epsg: int
    layer: str
    feature_count: int
    fields: tuple[tuple[str, str], ...]
    media_type: str
    writer_id: str  # science.VECTOR_WRITER_ID; part of the change version map
    pyogrio_version: str
    gdal_version: str
    sqlite_version_number: int  # from the SQLite file header (offset 96)


def features_from_events(events: Sequence[ChangeEvent]) -> list[EventFeature]:
    """Adapt change events to features, using their authoritative UTM geometry (never the 4326 copy)."""
    return [EventFeature(e.event_label, e.category, e.touches_aoi_boundary, e.geometry.geom_utm) for e in events]


def _is_utm(epsg: int) -> bool:
    return 32601 <= epsg <= 32660 or 32701 <= epsg <= 32760


def _check(features: Sequence[EventFeature], epsg: int) -> list[EventFeature]:
    if not _is_utm(epsg):
        raise InvariantViolation("vector_crs_not_utm", f"change_events.gpkg is written in the analysis UTM CRS, got EPSG:{epsg}")
    ordered = sorted(features, key=lambda f: f.event_label)
    labels = [f.event_label for f in ordered]
    if labels != list(range(1, len(ordered) + 1)):
        raise InvariantViolation("vector_event_labels", "global event_label values must be exactly 1..N (C-17)")
    names = set(CATEGORIES.values())
    for f in ordered:
        if f.category not in names:
            raise InvariantViolation("vector_category", f"event {f.event_label}: unknown category {f.category!r}")
        g = f.geometry
        if not isinstance(g, Polygon | MultiPolygon) or g.is_empty or not g.is_valid:
            raise InvariantViolation("vector_geometry", f"event {f.event_label}: geometry must be a valid Polygon/MultiPolygon")
        if g.has_z:
            raise InvariantViolation("vector_geometry", f"event {f.event_label}: geometry must be 2-D")
    return ordered


def write_change_events_gpkg(path: Path, features: Sequence[EventFeature], *, epsg: int) -> VectorArtifact:
    """Write ``change_events.gpkg`` deterministically to ``path`` (replaced if it exists)."""
    ordered = _check(features, epsg)
    multi = [g if isinstance(g, MultiPolygon) else MultiPolygon([g]) for g in (f.geometry for f in ordered)]
    wkb = np.array([shapely.to_wkb(g, output_dimension=2, byte_order=1) for g in multi], dtype=object)
    columns = {
        "event_label": np.array([f.event_label for f in ordered], dtype="int32"),
        "category": np.array([f.category for f in ordered], dtype=object),
        "touches_aoi_boundary": np.array([f.touches_aoi_boundary for f in ordered], dtype=bool),
    }
    field_data = [columns[name] for name, _ in FIELDS]
    path = Path(path)
    with _WRITE_LOCK:
        previous = pyogrio.get_gdal_config_option(_GDAL_CONFIG_KEY)
        pyogrio.set_gdal_config_options({_GDAL_CONFIG_KEY: PINNED_DATE})
        try:
            path.unlink(missing_ok=True)  # fresh file per write (G1)
            raw.write(
                str(path),
                wkb,
                field_data,
                [name for name, _ in FIELDS],
                layer=LAYER,
                driver=DRIVER,
                geometry_type=GEOMETRY_TYPE,
                crs=f"EPSG:{epsg}",
                encoding="UTF-8",
                layer_options=dict(LAYER_OPTIONS),
            )
        finally:
            pyogrio.set_gdal_config_options({_GDAL_CONFIG_KEY: previous})
    data = path.read_bytes()
    return VectorArtifact(
        path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
        crs_epsg=epsg,
        layer=LAYER,
        feature_count=len(ordered),
        fields=FIELDS,
        media_type=MEDIA_TYPE,
        writer_id=science.VECTOR_WRITER_ID,
        pyogrio_version=pyogrio.__version__,
        gdal_version=pyogrio.__gdal_version_string__,
        sqlite_version_number=int.from_bytes(data[96:100], "big"),
    )
