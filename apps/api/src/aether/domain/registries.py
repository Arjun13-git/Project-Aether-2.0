"""Code-owned registries (C7 §8): quantities and limitations.

A Measurement whose quantity is not registered is rejected before it reaches the database.
Quantities with a ``.qualifier`` suffix (e.g. ``items_excluded.baseline_unsupported``) belong
to a registered family and the qualifier must be in that family's allowed set.

Phase 2 (change runs) adds per-period variants: a quantity marked ``per_period`` also accepts a
trailing ``.a`` / ``.b`` (e.g. ``observations_usable.b``, ``masked_fraction.clear.a``); its
denominator then carries the same period when the denominator is itself per-period. Snapshot names
are unchanged (PHASE-2-RESOLUTION §E).
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
    per_period: bool = False  # also valid with a trailing ".a" / ".b" (change runs)
    allow_unqualified: bool = False  # a qualified family whose bare name is also valid (e.g. totals)


def _q(
    name: str,
    unit: str,
    stats: set[str],
    denominator: str | None = None,
    qualifiers: frozenset[str] | None = None,
    *,
    per_period: bool = False,
    allow_unqualified: bool = False,
) -> Quantity:
    return Quantity(name, unit, frozenset(stats), denominator, qualifiers, per_period, allow_unqualified)


_INDEX_NAMES = frozenset(science.INDICES)
_CHANGE_INDEX_NAMES = frozenset(science.CHANGE_INDICES)  # NDWI has no delta (C-06)
_CATEGORY_NAMES = frozenset(
    {"water_increase", "water_decrease", "built_bare_increase", "veg_loss", "veg_gain", "other_significant"}
)  # C5 §6
_FLAG_QUALIFIERS = frozenset(f"{i}.{s}" for i in science.CHANGE_INDICES for s in ("positive", "negative"))  # C-18

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
        _q("items_excluded", "count", {"count"}, qualifiers=EXCLUSION_REASONS, per_period=True),
        _q("tile_cloud_cover", "1", {"value"}),  # tile-level fraction from STAC metadata
        _q("sun_elevation", "deg", {"value"}),
        # Coverage, snapshot (C6 §1–§2, §4, §5.2)
        _q("observations_total", "count", {"count"}, per_period=True),
        _q("observations_usable", "count", {"count"}, per_period=True),
        _q("observed_area_a", "m2", {"value"}),
        _q("valid_area_a", "m2", {"value"}),
        _q("coverage_a", "1", {"fraction"}, denominator="aoi_grid_area"),
        _q("aoi_pixel_observations", "count", {"count"}, per_period=True),
        _q(
            "masked_fraction",
            "1",
            {"fraction"},
            denominator="aoi_pixel_observations",
            qualifiers=frozenset(MASK_REASONS),
            per_period=True,
        ),
        # Change runs: period B and joint coverage (C6 §1–§2; PHASE-2-RESOLUTION §E)
        _q("observed_area_b", "m2", {"value"}),
        _q("valid_area_b", "m2", {"value"}),
        _q("coverage_b", "1", {"fraction"}, denominator="aoi_grid_area"),
        _q("joint_valid_area", "m2", {"value"}),
        _q("joint_coverage", "1", {"fraction"}, denominator="aoi_grid_area"),
        # Delta statistics (C-06): Δ = B − A over joint_valid; support = joint_valid / AOI grid pixels
        _q("index_delta", "1", {"mean", "median", "p10", "p90"}, qualifiers=_CHANGE_INDEX_NAMES),
        _q("index_delta_pixels", "count", {"count"}, qualifiers=_CHANGE_INDEX_NAMES),
        _q(
            "index_delta_support",
            "1",
            {"fraction"},
            denominator="aoi_grid_pixels_10m",
            qualifiers=_CHANGE_INDEX_NAMES,
        ),
        # Co-registration check (C3 §9, C-01/C-02); scalars only
        _q("grid_pixels_10m", "count", {"count"}),
        _q("coreg_residual", "px", {"value"}),
        _q("coreg_shift_x", "px", {"value"}),
        _q("coreg_shift_y", "px", {"value"}),
        _q("coreg_pcc_error", "1", {"value"}),
        _q("coreg_qa_nir_std", "1", {"value"}, per_period=True),
        _q("coreg_window_width", "px", {"value"}),
        _q("coreg_window_height", "px", {"value"}),
        _q("coreg_grid_joint_valid_fraction", "1", {"fraction"}, denominator="grid_pixels_10m"),
        # Change events (C5 §8, C-18)
        _q("event_area", "m2", {"value"}),
        _q("event_pixel_count", "count", {"count"}),
        _q("event_median_delta", "1", {"median"}, qualifiers=_CHANGE_INDEX_NAMES),
        _q("event_strength", "1", {"median"}),
        _q("event_support_a", "count", {"median"}),
        _q("event_support_b", "count", {"median"}),
        _q("event_flag_pixels", "count", {"count"}, qualifiers=_FLAG_QUALIFIERS),
        # Change summary (C5 §10, C6 §1–§2; C-17 union areas)
        _q("change_event_count", "count", {"count"}, qualifiers=_CATEGORY_NAMES, allow_unqualified=True),
        _q("changed_area", "m2", {"value"}, qualifiers=_CATEGORY_NAMES, allow_unqualified=True),
        _q(
            "changed_fraction",
            "1",
            {"fraction"},
            denominator="joint_valid_area",
            qualifiers=_CATEGORY_NAMES,
            allow_unqualified=True,
        ),
        _q("eligible_pixels", "count", {"count"}),
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


def _resolve_exact(name: str) -> Quantity:
    if name in QUANTITIES and (QUANTITIES[name].qualifiers is None or QUANTITIES[name].allow_unqualified):
        return QUANTITIES[name]
    family, _, qualifier = name.partition(".")
    entry = QUANTITIES.get(family)
    if entry is None or entry.qualifiers is None or not qualifier:
        raise InvariantViolation("unknown_quantity", f"quantity {name!r} is not registered")
    base = qualifier.split(":", 1)[0]
    if base not in entry.qualifiers:
        raise InvariantViolation("unknown_quantity", f"qualifier {qualifier!r} not allowed for {family}")
    return entry


def _split_period(name: str) -> tuple[str, str | None]:
    if name.endswith((".a", ".b")):
        return name[:-2], name[-1]
    return name, None


def resolve_quantity(name: str) -> Quantity:
    """Return the registry entry for ``name`` (family-aware, per-period aware) or raise."""
    try:
        return _resolve_exact(name)
    except InvariantViolation:
        base, period = _split_period(name)
        if period is None:
            raise
        entry = _resolve_exact(base)
        if not entry.per_period:
            raise InvariantViolation("unknown_quantity", f"quantity {base!r} has no per-period variant") from None
        return entry


def denominator_for(name: str) -> str | None:
    """Denominator quantity name: qualified like the numerator when the family is qualified, and with
    the numerator's period when the denominator is per-period."""
    entry = resolve_quantity(name)
    if entry.denominator is None:
        return None
    try:
        _resolve_exact(name)
        base, period = name, None
    except InvariantViolation:
        base, period = _split_period(name)
    _, _, qualifier = base.partition(".")
    denom_entry = QUANTITIES[entry.denominator]
    out = f"{entry.denominator}.{qualifier}" if denom_entry.qualifiers and qualifier else entry.denominator
    return f"{out}.{period}" if period is not None and denom_entry.per_period else out


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
