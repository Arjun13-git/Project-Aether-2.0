"""Versioned scientific configuration (single source of truth).

Every value here is defined by a locked Phase 0.5 contract (System-Design/03-geospatial/C1–C6).
Changing any value requires a contract change and a version bump (invariant I-17). Values marked
``UNTUNED`` are deterministic engineering defaults, not calibrated thresholds (C5 §9, C6 §5, I-20).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

# --- Algorithm / contract versions ------------------------------------------------------------

INGEST_VERSION: Final = "ingest.earth_search.v1"  # C1
RADIOMETRY_VERSION: Final = "radiometry.s2c1.v1"  # C2
GRID_VERSION: Final = "grid.utm_s2.v1"  # C3
MASK_VERSION: Final = "mask.scl.v1"  # C4 §2
COMPOSITE_VERSION: Final = "composite.median_index.v1"  # C4 §3
INDICES_VERSION: Final = "indices.s2.v1"  # C4 §4
COVERAGE_VERSION: Final = "coverage.v1"  # C6
ZONAL_STATS_METHOD: Final = "zonal_stats@1"  # C4 §5
GEODESIC_AREA_METHOD: Final = "geodesic_area@1"  # C3 §1
PIXEL_AREA_METHOD: Final = "pixel_area@1"  # C3 §7
EVIDENCE_SCHEMA_VERSION: Final = "evidence.v1"  # C7

# Phase 2 versions (C5, C3 §9). Used only by change runs; never part of the snapshot map.
CHANGE_VERSION: Final = "change.index_diff.v1"  # C5
COREG_VERSION: Final = "coreg.phase_corr.v1"  # C3 §9
VECTOR_WRITER_ID: Final = "vector.gpkg.det1"  # implementation id of the deterministic GeoPackage writer (G1)

# Snapshot runs (Phase 1). Frozen: entries, values and order are part of the snapshot idempotency key
# and of stored provenance (JOB-SYSTEM, Phase 2 freeze C-10). Never add Phase 2 entries here.
SNAPSHOT_ALGORITHM_VERSIONS: Final[dict[str, str]] = {
    "ingest": INGEST_VERSION,
    "radiometry": RADIOMETRY_VERSION,
    "grid": GRID_VERSION,
    "mask": MASK_VERSION,
    "composite": COMPOSITE_VERSION,
    "indices": INDICES_VERSION,
    "coverage": COVERAGE_VERSION,
    "zonal_stats": ZONAL_STATS_METHOD,
}

# Backwards-compatible alias: Phase 1 callers and stored records use this name for the snapshot map.
ALGORITHM_VERSIONS: Final[dict[str, str]] = SNAPSHOT_ALGORITHM_VERSIONS

# Change runs (Phase 2): the snapshot map plus the change-specific versions. For future change runs only.
CHANGE_ALGORITHM_VERSIONS: Final[dict[str, str]] = {
    **SNAPSHOT_ALGORITHM_VERSIONS,
    "change": CHANGE_VERSION,
    "coreg": COREG_VERSION,
    "vector": VECTOR_WRITER_ID,
}


class Calibration(StrEnum):
    UNTUNED = "untuned"
    PRODUCT_CHOICE = "product_choice"
    CONTRACT_FIXED = "contract_fixed"


# --- C1: source -------------------------------------------------------------------------------

STAC_COLLECTION: Final = "sentinel-2-c1-l2a"
ASSET_HREF_PREFIX: Final = "https://e84-earth-search-sentinel-data.s3.us-west-2.amazonaws.com/sentinel-2-c1-l2a/"
MIN_PROCESSING_BASELINE: Final = (5, 0)
MAX_TILE_CLOUD_DEFAULT: Final = 80.0  # C1 §4 pre-filter on tile-level cloud, not AOI cloud
MAX_ITEMS_PER_PERIOD: Final = 500
ASSET_READ_RETRIES: Final = 3
ASSET_READ_BACKOFF_S: Final = (1.0, 2.0, 4.0)


@dataclass(frozen=True)
class AssetSpec:
    key: str
    band: str
    native_res_m: int
    kind: str  # "reflectance" | "scl"
    central_wavelength_nm: float | None


# STAC asset keys are authoritative (C1 §3). Wavelengths are Sentinel-2A central values (ESA).
ASSETS: Final[dict[str, AssetSpec]] = {
    "blue": AssetSpec("blue", "B02", 10, "reflectance", 492.4),
    "green": AssetSpec("green", "B03", 10, "reflectance", 559.8),
    "red": AssetSpec("red", "B04", 10, "reflectance", 664.6),
    "nir": AssetSpec("nir", "B08", 10, "reflectance", 832.8),
    "swir16": AssetSpec("swir16", "B11", 20, "reflectance", 1613.7),
    "scl": AssetSpec("scl", "SCL", 20, "scl", None),
}
REQUIRED_ASSETS: Final = tuple(ASSETS)  # order matters for deterministic provenance

# --- C2: radiometry ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RadiometryAllowList:
    data_type: str
    scale: float | None
    offset: float | None
    nodata: int


RADIOMETRY_ALLOW: Final[dict[str, RadiometryAllowList]] = {
    "reflectance": RadiometryAllowList("uint16", 0.0001, -0.1, 0),
    "scl": RadiometryAllowList("uint8", None, None, 0),
}
SATURATED_DN: Final = 65535

# --- C3: grid / AOI ---------------------------------------------------------------------------

AOI_MAX_AREA_M2: Final = 100_000_000.0  # 100 km²
AOI_MAX_VERTICES: Final = 1000
AOI_MAX_ABS_LAT: Final = 80.0
AOI_REPAIR_MAX_AREA_CHANGE: Final = 0.001  # make_valid accepted only if area changes < 0.1 %
AOI_COORD_ROUND_DEG: Final = 9  # 1e-9° (C7 §3)
GRID_BUFFER_M: Final = 200.0
GRID_SNAP_M: Final = 20
GRID_MAX_PX_10M: Final = 2000
UTM_EXCEPTION_REGIONS: Final = (
    ("norway", (3.0, 56.0, 12.0, 64.0)),
    ("svalbard", (0.0, 72.0, 42.0, 84.0)),
)

# --- C4: periods, mask, composite, indices -----------------------------------------------------

PERIOD_MIN_DAYS: Final = 30
PERIOD_MAX_DAYS: Final = 120
SEASON_MATCH_MAX_DOY: Final = 30
ARCHIVE_START: Final = "2017-01-01"


@dataclass(frozen=True)
class MaskProfile:
    version: str
    valid_classes: frozenset[int]
    dilated_classes: frozenset[int]
    dilation_radius_px_20m: int
    snow_class: int


SCL_MASK: Final = MaskProfile(
    version=MASK_VERSION,
    valid_classes=frozenset({4, 5, 6}),
    dilated_classes=frozenset({3, 8, 9, 10}),
    dilation_radius_px_20m=3,
    snow_class=11,
)

SCL_LABELS: Final[dict[int, str]] = {
    0: "NO_DATA",
    1: "SATURATED_OR_DEFECTIVE",
    2: "CAST_SHADOWS",
    3: "CLOUD_SHADOWS",
    4: "VEGETATION",
    5: "NOT_VEGETATED",
    6: "WATER",
    7: "UNCLASSIFIED",
    8: "CLOUD_MEDIUM_PROBABILITY",
    9: "CLOUD_HIGH_PROBABILITY",
    10: "THIN_CIRRUS",
    11: "SNOW_OR_ICE",
}


@dataclass(frozen=True)
class Parameter:
    name: str
    value: float | int
    calibration: Calibration
    note: str


N_MIN: Final = Parameter("n_min", 3, Calibration.UNTUNED, "Initial engineering default (C5 §9)")
INDEX_DENOM_EPS: Final = 1e-4  # C4 §4: undefined if a + b ≤ 1e-4
STATS_MIN_SUPPORT: Final = Parameter("stats_min_support_fraction", 0.5, Calibration.UNTUNED, "C4 §5 unavailable rule")


@dataclass(frozen=True)
class IndexSpec:
    name: str
    a: str  # asset key of the first term in (a − b) / (a + b)
    b: str
    grid: str  # "G10" | "G20"
    formula: str
    correlate: str


# indices.s2.v1 — the frozen MVP set (C4 §4). BSI/NBR/SAVI/EVI are post-MVP (owner decision
# 2026-10-13: keep the frozen set).
INDICES: Final[dict[str, IndexSpec]] = {
    "ndvi": IndexSpec("ndvi", "nir", "red", "G10", "(nir - red) / (nir + red)", "vegetation"),
    "ndwi": IndexSpec("ndwi", "green", "nir", "G10", "(green - nir) / (green + nir)", "open water (McFeeters)"),
    "mndwi": IndexSpec("mndwi", "green", "swir16", "G20", "(green20 - swir16) / (green20 + swir16)", "open water"),
    "ndbi": IndexSpec(
        "ndbi",
        "swir16",
        "nir",
        "G20",
        "(swir16 - nir20) / (swir16 + nir20)",
        "built-up/bare surfaces (weak proxy)",
    ),
}

DISPLAY_RGB_STRETCH: Final = (0.0, 0.3)  # display only (C4 §3)

# --- C6: coverage -----------------------------------------------------------------------------

COVERAGE_INSUFFICIENT_BELOW: Final = Parameter("coverage_insufficient_below", 0.5, Calibration.UNTUNED, "C6 §5 policy default")
COVERAGE_PARTIAL_BELOW: Final = Parameter("coverage_partial_below", 0.8, Calibration.UNTUNED, "C6 §5 policy default")


def snapshot_parameters() -> dict[str, object]:
    """Canonical parameter set of a Phase 1 snapshot run (part of the idempotency key)."""
    return {
        "n_min": N_MIN.value,
        "max_tile_cloud": MAX_TILE_CLOUD_DEFAULT,
        "mask_profile": MASK_VERSION,
        "indices": sorted(INDICES),
        "stats_min_support_fraction": STATS_MIN_SUPPORT.value,
        "coverage_thresholds": [COVERAGE_INSUFFICIENT_BELOW.value, COVERAGE_PARTIAL_BELOW.value],
    }


def parameter_calibration_table() -> list[dict[str, object]]:
    """Disclosure table: every numeric threshold with its calibration status (I-20)."""
    params = (N_MIN, STATS_MIN_SUPPORT, COVERAGE_INSUFFICIENT_BELOW, COVERAGE_PARTIAL_BELOW)
    return [{"name": p.name, "value": p.value, "calibration": str(p.calibration), "note": p.note} for p in params]
