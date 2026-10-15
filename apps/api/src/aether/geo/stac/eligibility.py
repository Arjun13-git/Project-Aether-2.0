"""Scene parsing, eligibility E1–E7, duplicates and observations (C1 §5–§7).

Every item that is not used gets a reason code; nothing is dropped silently.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from shapely.geometry import shape

from aether import science
from aether.geo.radiometry import AssetRadiometry, validate_stac_metadata


@dataclass(frozen=True)
class AssetRecord:
    key: str
    href: str
    media_type: str | None
    raster_bands: list[dict[str, Any]] | None
    file_checksum: str | None
    file_size: int | None
    proj_transform: list[float] | None
    proj_shape: list[int] | None


@dataclass
class SceneRecord:
    item_id: str
    collection: str
    acquired_at: datetime | None
    platform: str
    processing_baseline: str | None
    datatake_id: str | None
    grid_code: str | None
    epsg: int | None
    generation_time: str | None
    tile_cloud_cover: float | None
    sun_elevation_deg: float | None
    sun_azimuth_deg: float | None
    incidence_angle_deg: float | None
    footprint: dict[str, Any] | None
    stac_updated: str | None
    stac_json: dict[str, Any]
    stac_sha256: str
    assets: dict[str, AssetRecord]
    radiometry: dict[str, AssetRadiometry] = field(default_factory=dict)
    status: str = "pending"  # used | excluded
    reason_code: str | None = None


def canonical_sha256(obj: Any) -> str:
    """sha256 over canonical JSON: sorted keys, UTF-8, no whitespace (C1 §8)."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _epsg(props: dict[str, Any]) -> int | None:
    if isinstance(props.get("proj:epsg"), int):
        return props["proj:epsg"]
    code = props.get("proj:code")
    if isinstance(code, str) and code.upper().startswith("EPSG:"):
        try:
            return int(code.split(":", 1)[1])
        except ValueError:
            return None
    return None


def _float(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse_item(item: dict[str, Any]) -> SceneRecord:
    props = item.get("properties", {}) or {}
    dt = props.get("datetime")
    acquired = None
    if isinstance(dt, str):
        try:
            acquired = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            acquired = None
    assets: dict[str, AssetRecord] = {}
    for key, a in (item.get("assets") or {}).items():
        if key not in science.ASSETS:
            continue  # C1 §3: other assets are never read
        assets[key] = AssetRecord(
            key=key,
            href=str(a.get("href", "")),
            media_type=a.get("type"),
            raster_bands=a.get("raster:bands"),
            file_checksum=a.get("file:checksum"),
            file_size=a.get("file:size") if isinstance(a.get("file:size"), int) else None,
            proj_transform=a.get("proj:transform"),
            proj_shape=a.get("proj:shape"),
        )
    return SceneRecord(
        item_id=str(item.get("id")),
        collection=str(item.get("collection")),
        acquired_at=acquired,
        platform=str(props.get("platform") or "unknown"),
        processing_baseline=props.get("s2:processing_baseline"),
        datatake_id=props.get("s2:datatake_id"),
        grid_code=props.get("grid:code"),
        epsg=_epsg(props),
        generation_time=props.get("s2:generation_time"),
        tile_cloud_cover=_float(props.get("eo:cloud_cover")),
        sun_elevation_deg=_float(props.get("view:sun_elevation")),
        sun_azimuth_deg=_float(props.get("view:sun_azimuth")),
        incidence_angle_deg=_float(props.get("view:incidence_angle")),
        footprint=item.get("geometry"),
        stac_updated=props.get("updated"),
        stac_json=item,
        stac_sha256=canonical_sha256(item),
        assets=assets,
    )


def baseline_tuple(b: str | None) -> tuple[int, int] | None:
    if not isinstance(b, str):
        return None
    try:
        major, minor = b.split(".", 1)
        return int(major), int(minor)
    except ValueError:
        return None


def check_item(scene: SceneRecord, grid_epsg: int) -> str | None:
    """Rules E1–E6 in order (E7 duplicates are resolved across items). Returns a reason or None."""
    if scene.collection != science.STAC_COLLECTION:
        return "wrong_collection"  # E1
    bt = baseline_tuple(scene.processing_baseline)
    if bt is None or bt < science.MIN_PROCESSING_BASELINE:
        return "baseline_unsupported"  # E2
    if scene.acquired_at is None:
        return "datetime_missing"
    if scene.epsg != grid_epsg:
        return "different_utm_zone"  # E3
    for key in science.REQUIRED_ASSETS:
        if key not in scene.assets:
            return f"asset_missing:{key}"  # E4
    for key in science.REQUIRED_ASSETS:
        if not scene.assets[key].href.startswith(science.ASSET_HREF_PREFIX):
            return "href_not_allowed"  # E5
    for key in science.REQUIRED_ASSETS:
        result = validate_stac_metadata(key, {"raster:bands": scene.assets[key].raster_bands})
        if isinstance(result, str):
            return result  # E6
        scene.radiometry[key] = result
    if not scene.datatake_id:
        # Not named by C1 (s2:datatake_id was present on every verified item). Observations are
        # datatakes (C1 §6), so an item without one cannot be grouped: excluded, never guessed.
        return "datatake_missing"
    return None


def resolve_duplicates(scenes: list[SceneRecord]) -> None:
    """E7: same grid:code + datatake → keep highest baseline, then latest generation time."""
    groups: dict[tuple[str | None, str | None], list[SceneRecord]] = defaultdict(list)
    for s in scenes:
        if s.status == "pending":
            groups[(s.grid_code, s.datatake_id)].append(s)
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(
            key=lambda s: (baseline_tuple(s.processing_baseline) or (0, 0), s.generation_time or "", s.item_id),
            reverse=True,
        )
        for loser in members[1:]:
            loser.status, loser.reason_code = "excluded", "duplicate_superseded"


def apply_eligibility(scenes: list[SceneRecord], grid_epsg: int) -> None:
    for s in scenes:
        reason = check_item(s, grid_epsg)
        if reason is not None:
            s.status, s.reason_code = "excluded", reason
    resolve_duplicates(scenes)
    for s in scenes:
        if s.status == "pending":
            s.status = "used"


@dataclass(frozen=True)
class Observation:
    """One datatake over the AOI; mosaics its items in ``id`` order (C1 §6)."""

    datatake_id: str
    items: tuple[SceneRecord, ...]

    @property
    def acquired_at(self) -> datetime:
        return min(s.acquired_at for s in self.items if s.acquired_at is not None)

    @property
    def platform(self) -> str:
        return self.items[0].platform


def group_observations(scenes: list[SceneRecord]) -> list[Observation]:
    by_dt: dict[str, list[SceneRecord]] = defaultdict(list)
    for s in scenes:
        if s.status == "used" and s.datatake_id:
            by_dt[s.datatake_id].append(s)
    obs = [Observation(dt, tuple(sorted(items, key=lambda s: s.item_id))) for dt, items in by_dt.items()]
    return sorted(obs, key=lambda o: (o.acquired_at, o.datatake_id))


def exclusion_counts(scenes: list[SceneRecord]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for s in scenes:
        if s.status == "excluded" and s.reason_code:
            counts[s.reason_code] += 1
    return dict(sorted(counts.items()))


def footprint_shape(scene: SceneRecord) -> Any:
    return shape(scene.footprint) if scene.footprint else None
