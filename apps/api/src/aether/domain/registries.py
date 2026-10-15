"""Code-owned registries (C7 §8): quantities and limitations.

A Measurement whose quantity is not registered is rejected before it reaches the database.
Quantities with a ``.qualifier`` suffix (e.g. ``items_excluded.baseline_unsupported``) belong
to a registered family and the qualifier must be in that family's allowed set.
"""

from __future__ import annotations

from dataclasses import dataclass

from aether import science
from aether.domain.errors import InvariantViolation

STATISTICS = frozenset({"value", "mean", "median", "p10", "p90", "count", "sum", "fraction"})
UNITS = frozenset({"m2", "m", "count", "1", "deg", "px"})
UNAVAILABLE_REASONS = frozenset(
    {
        "insufficient_observations",
        "insufficient_coverage",
        "insufficient_support",
        "coregistration_failed",
        "robust_reference_unavailable",
        "not_applicable",
    }
)

# Item exclusion reason codes (C1 §5, C2 §3, C3 §4). `asset_missing:<key>` and
# `asset_unreadable:<key>` are families.
EXCLUSION_REASONS = frozenset(
    {
        "wrong_collection",
        "baseline_unsupported",
        "different_utm_zone",
        "href_not_allowed",
        "radiometry_metadata_missing",
        "radiometry_metadata_unexpected",
        "radiometry_metadata_inconsistent",
        "radiometry_dtype_mismatch",
        "duplicate_superseded",
        "datetime_missing",
        "datatake_missing",
        "grid_misaligned",
        "asset_missing",
        "asset_unreadable",
    }
)

# Masked pixel-observation reasons used in coverage evidence (C6 §4).
MASK_REASONS = ("clear", "nodata", "cloud_shadow", "snow_ice", "other_invalid")


@dataclass(frozen=True)
class Quantity:
    name: str
    unit: str
    statistics: frozenset[str]
    denominator: str | None = None  # required for statistic "fraction"
    qualifiers: frozenset[str] | None = None  # family members allowed after "."


def _q(
    name: str, unit: str, stats: set[str], denominator: str | None = None, qualifiers: frozenset[str] | None = None
) -> Quantity:
    return Quantity(name, unit, frozenset(stats), denominator, qualifiers)


_INDEX_NAMES = frozenset(science.INDICES)

QUANTITIES: dict[str, Quantity] = {
    q.name: q
    for q in [
        # AOI (C3 §1, C6 §1)
        _q("aoi_area", "m2", {"value"}),
        _q("aoi_grid_area", "m2", {"value"}),
        _q("aoi_grid_pixels_10m", "count", {"count"}),
        _q("aoi_grid_pixels_20m", "count", {"count"}),
        _q("grid_vs_vector_area_ratio", "1", {"fraction"}, denominator="aoi_area"),
        # Catalogue / scenes (C7 §5)
        _q("items_matched", "count", {"count"}),
        _q("items_considered", "count", {"count"}),
        _q("items_used", "count", {"count"}),
        _q("items_excluded", "count", {"count"}, qualifiers=EXCLUSION_REASONS),
        _q("tile_cloud_cover", "1", {"value"}),  # tile-level fraction from STAC metadata
        _q("sun_elevation", "deg", {"value"}),
        # Coverage, snapshot (C6 §1–§2, §4, §5.2)
        _q("observations_total", "count", {"count"}),
        _q("observations_usable", "count", {"count"}),
        _q("observed_area_a", "m2", {"value"}),
        _q("valid_area_a", "m2", {"value"}),
        _q("coverage_a", "1", {"fraction"}, denominator="aoi_grid_area"),
        _q("aoi_pixel_observations", "count", {"count"}),
        _q(
            "masked_fraction",
            "1",
            {"fraction"},
            denominator="aoi_pixel_observations",
            qualifiers=frozenset(MASK_REASONS),
        ),
        # Index statistics (C4 §5); qualifier = index name
        _q("index", "1", {"mean", "median", "p10", "p90"}, qualifiers=_INDEX_NAMES),
        _q("index_pixels", "count", {"count"}, qualifiers=_INDEX_NAMES),
        _q("index_region_pixels", "count", {"count"}, qualifiers=_INDEX_NAMES),
        _q(
            "index_support",
            "1",
            {"fraction"},
            denominator="index_region_pixels",
            qualifiers=_INDEX_NAMES,
        ),
    ]
}


def resolve_quantity(name: str) -> Quantity:
    """Return the registry entry for ``name`` (family-aware) or raise."""
    if name in QUANTITIES and QUANTITIES[name].qualifiers is None:
        return QUANTITIES[name]
    family, _, qualifier = name.partition(".")
    entry = QUANTITIES.get(family)
    if entry is None or entry.qualifiers is None or not qualifier:
        raise InvariantViolation("unknown_quantity", f"quantity {name!r} is not registered")
    base = qualifier.split(":", 1)[0]
    if base not in entry.qualifiers:
        raise InvariantViolation("unknown_quantity", f"qualifier {qualifier!r} not allowed for {family}")
    return entry


def denominator_for(name: str) -> str | None:
    """Denominator quantity name (qualified like the numerator when the family is qualified)."""
    entry = resolve_quantity(name)
    if entry.denominator is None:
        return None
    _, _, qualifier = name.partition(".")
    denom_entry = QUANTITIES[entry.denominator]
    return f"{entry.denominator}.{qualifier}" if denom_entry.qualifiers and qualifier else entry.denominator


@dataclass(frozen=True)
class Limitation:
    code: str
    blocking: bool
    text: str


LIMITATIONS: dict[str, Limitation] = {
    lim.code: lim
    for lim in [
        Limitation(
            "partial_coverage",
            True,
            "Only part of the AOI had enough clear observations; results describe the covered part only.",
        ),
        Limitation(
            "scl_bright_surface_confusion",
            False,
            "The Sentinel-2 scene classification can flag bright built surfaces as cloud.",
        ),
        Limitation(
            "scl_thin_cloud_omission",
            False,
            "Thin cloud or haze can pass the scene classification as clear.",
        ),
        Limitation(
            "index_resolution_20m",
            False,
            "This index is computed on the 20 m grid; boundaries are coarser than 10 m layers.",
        ),
        Limitation(
            "ndbi_not_builtup_detector",
            False,
            "NDBI is a weak proxy for built-up or bare surfaces, not a built-up detector.",
        ),
        # Registered for Phase 2 producers; no Phase 1 code emits these.
        Limitation("coregistration_not_verified", True, "Co-registration could not be verified."),
        Limitation("widespread_change", True, "Change is widespread; robust reference unusable."),
        Limitation("robust_reference_unavailable", True, "Robust reference statistics unavailable."),
        Limitation("event_truncated_by_aoi", False, "The event touches the AOI boundary."),
        Limitation(
            "thresholds_untuned",
            False,
            "Change thresholds are untuned engineering defaults, not calibrated values.",
        ),
    ]
}


def limitation(code: str) -> dict[str, object]:
    if code not in LIMITATIONS:
        raise InvariantViolation("unknown_limitation", f"limitation {code!r} is not registered")
    lim = LIMITATIONS[code]
    return {"code": lim.code, "blocking": lim.blocking}
