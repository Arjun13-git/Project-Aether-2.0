"""Change events per category (C5 §7) and their measurements and confidence bands (C5 §8).

Per category code c ∈ 1..6, on G10:
  mask_c → opening/closing ∧ joint_valid → 8-connected components → MMU → pixel-edge polygons.
An event is one retained 8-connected component of one category. Its identity is
``(category_code, label)`` where ``label`` is the raster-scan component number within that
category after MMU. Events are ordered by that pair.

Categories are cleaned independently, exactly as C5 §7 specifies, and each category keeps its
own label raster: no cross-category merging, priority or overlap resolution is applied (C-17).
A pixel may therefore belong to events of several categories; such a pixel is never in either
category's opened mask (both closings restored or filled it).
Global ``event_label`` = 1..N in ``(category_code, label)`` order; the 6-band event-label raster
holds band c = global labels of category c. ``changed_area`` is the union of event pixels and
``changed_area_<category>`` the union within the category (C6 §1, C-17).

Per-index flags (C-18): per event and index, the counts of pixels with s_I = +1 and −1 (zero is
derived) and the sign-set fact ``index_flags`` (signs occurring, order "-", "0", "+"). These are
descriptive counts of the per-pixel sign layers, not an event-wide significance classification.

Per-event measurements (C5 §8): pixel count, area = n_px × pixel_area_true_m2, median Δ per change
index, strength = median over event pixels of max_I |z_I| (robust mode only), support A/B = median
n_valid(NDVI). Boundary flag (C5 §7 step 5): an event pixel 8-adjacent to a non-in-AOI pixel.

Confidence bands are rule-based data-quality indicators, not calibrated probabilities.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from scipy import ndimage

from aether import science
from aether.domain.errors import InvariantViolation
from aether.geo.change.delta import IndexDelta
from aether.geo.change.joint import JointValid
from aether.geo.change.morphology import CONNECTIVITY_8, apply_mmu, clean_mask, label_components, mmu_min_pixels
from aether.geo.change.polygons import EventGeometry, polygonize_components
from aether.geo.change.robust import RobustReference
from aether.geo.change.rules import CATEGORIES
from aether.geo.composite import IndexComposite
from aether.geo.coreg import CoregResult
from aether.geo.grid import GridArray, require_same_grid

Band = Literal["higher", "moderate", "lower"]
SIGN_ORDER: tuple[str, ...] = ("-", "0", "+")  # C-18 fixed order


@dataclass(frozen=True)
class CategoryLayer:
    """Retained components of one category: int32 labels 1..kept on G10 (0 = none)."""

    code: int
    category: str
    labels: GridArray
    components: int
    kept: int
    removed: int


@dataclass(frozen=True)
class ChangeEvent:
    category_code: int
    category: str
    label: int  # component number within its category (raster-scan order after MMU)
    event_label: int  # global 1..N in (category_code, label) order (C-17)
    pixel_count: int
    area_m2: float
    median_delta: dict[str, float]
    strength: float | None
    strength_unavailable_reason: str | None
    support_a: float
    support_b: float
    confidence_band: Band
    touches_aoi_boundary: bool
    limitations: tuple[str, ...]  # event-specific (non-blocking); run-level blocking set lives on the run
    geometry: EventGeometry
    flag_pixels: dict[str, dict[str, int]]  # C-18 measurements: {index: {"positive": n, "negative": n}}
    index_flags: dict[str, list[str]]  # C-18 fact: signs occurring per index, order "-", "0", "+"

    def flag_zero_pixels(self, index: str) -> int:
        """C-18 derived zero count: event_pixel_count − positive − negative."""
        c = self.flag_pixels[index]
        return self.pixel_count - c["positive"] - c["negative"]


@dataclass(frozen=True)
class EventsResult:
    layers: tuple[CategoryLayer, ...]
    events: tuple[ChangeEvent, ...]
    mmu_min_px: int
    steps: tuple[dict[str, Any], ...]
    event_label_bands: tuple[GridArray, ...]  # 6 int32 bands, category codes 1..6; global labels, 0 = none
    changed_px: int  # union of all retained event pixels (C6 §1, C-17)
    changed_px_by_category: dict[str, int]  # union within each category
    pixel_area_true_m2: float

    @property
    def changed_area_m2(self) -> float:
        return self.changed_px * self.pixel_area_true_m2

    def changed_area_category_m2(self, category: str) -> float:
        return self.changed_px_by_category[category] * self.pixel_area_true_m2

    @property
    def overlap_px(self) -> int:
        """Σ per-category union − overall union = cross-category overlap pixels (C-17)."""
        return sum(self.changed_px_by_category.values()) - self.changed_px


def _flag_pixels(sig: Mapping[str, np.ndarray], px: np.ndarray) -> tuple[dict[str, dict[str, int]], dict[str, list[str]]]:
    counts: dict[str, dict[str, int]] = {}
    flags: dict[str, list[str]] = {}
    n = int(px.sum())
    for name in science.CHANGE_INDICES:
        vals = sig[name][px]
        pos, neg = int((vals == 1).sum()), int((vals == -1).sum())
        counts[name] = {"positive": pos, "negative": neg}
        present = {"-": neg > 0, "0": n - pos - neg > 0, "+": pos > 0}
        flags[name] = [sym for sym in SIGN_ORDER if present[sym]]
    return counts, flags


def confidence_band(ref: RobustReference, strength: float | None, support_a: float, support_b: float, coreg: CoregResult) -> Band:
    """C5 §8. Forced ``lower`` in absolute_only mode or when co-registration is not assessable."""
    if ref.mode != "robust" or coreg.status != "ok" or strength is None:
        return "lower"
    support = min(support_a, support_b)
    residual = coreg.residual_px
    if (
        strength >= science.BAND_HIGHER_MIN_STRENGTH.value
        and support >= science.BAND_HIGHER_MIN_SUPPORT.value
        and residual is not None
        and residual <= science.BAND_HIGHER_MAX_COREG_RESIDUAL_PX.value
    ):
        return "higher"
    if strength >= science.BAND_MODERATE_MIN_STRENGTH.value and support >= science.BAND_MODERATE_MIN_SUPPORT.value:
        return "moderate"
    return "lower"


def extract_events(
    *,
    categories: GridArray,
    joint: JointValid,
    deltas: dict[str, IndexDelta],
    sig: Mapping[str, np.ndarray],
    ref: RobustReference,
    ndvi_a: IndexComposite,
    ndvi_b: IndexComposite,
    in_aoi10: GridArray,
    coreg: CoregResult,
    pixel_area_true_m2: float,
) -> EventsResult:
    g10 = require_same_grid(categories, joint.grid, in_aoi10, ndvi_a.array, ndvi_b.array)
    if coreg.status == "failed":
        raise InvariantViolation("change_after_coreg_failure", "change detection does not run after coregistration_failed")
    min_px = mmu_min_pixels(science.CHANGE_MMU_M2.value, pixel_area_true_m2)
    outside = ~in_aoi10.data
    near_outside = ndimage.binary_dilation(outside, structure=CONNECTIVITY_8.astype(bool))
    if sorted(sig) != sorted(science.CHANGE_INDICES) or any(v.shape != g10.shape or v.dtype != np.int8 for v in sig.values()):
        raise InvariantViolation("grid_mismatch", "significance layers must be int8 G10 rasters for every change index")
    max_abs_z = None
    if ref.mode == "robust" and ref.z is not None:
        max_abs_z = np.max(np.stack([np.abs(ref.z[n]) for n in science.CHANGE_INDICES]), axis=0)

    layers: list[CategoryLayer] = []
    events: list[ChangeEvent] = []
    poly_steps: list[dict[str, Any]] = []
    for code, name in CATEGORIES.items():
        mask = GridArray(g10, categories.data == code, joint.mask.copy())
        cleaned = clean_mask(mask, joint)
        labels, n = label_components(cleaned, g10)
        kept, k, removed = apply_mmu(labels, n, min_px, g10)
        layers.append(CategoryLayer(code, name, kept, n, k, removed))
        geoms, pstep = polygonize_components(kept, g10)
        poly_steps.append({**pstep, "category": name})
        for geom in geoms:
            px = kept.data == geom.label
            n_px = int(px.sum())
            if n_px != geom.pixel_count:
                raise InvariantViolation("event_pixel_count", f"{name} event {geom.label}: geometry and raster disagree")
            strength = float(np.median(max_abs_z[px])) if max_abs_z is not None else None
            sa = float(np.median(ndvi_a.n_valid[px]))
            sb = float(np.median(ndvi_b.n_valid[px]))
            touches = bool((near_outside & px).any())
            flag_px, flags = _flag_pixels(sig, px)
            events.append(
                ChangeEvent(
                    category_code=code,
                    category=name,
                    label=geom.label,
                    event_label=len(events) + 1,
                    pixel_count=n_px,
                    area_m2=n_px * pixel_area_true_m2,
                    median_delta={i: float(np.median(deltas[i].g10.data[px])) for i in science.CHANGE_INDICES},
                    strength=strength,
                    strength_unavailable_reason=None if strength is not None else "robust_reference_unavailable",
                    support_a=sa,
                    support_b=sb,
                    confidence_band=confidence_band(ref, strength, sa, sb, coreg),
                    touches_aoi_boundary=touches,
                    limitations=("thresholds_untuned", *(("event_truncated_by_aoi",) if touches else ())),
                    geometry=geom,
                    flag_pixels=flag_px,
                    index_flags=flags,
                )
            )
    morph_step = {
        "kind": "morphology",
        "version": science.CHANGE_VERSION,
        "structuring_element": "square_3x3",
        "sequence": ["opening", "closing", "and_joint_valid", "label_8_connected", "mmu"],
        "connectivity": 8,
        "mmu_m2": float(science.CHANGE_MMU_M2.value),
        "mmu_min_px": min_px,
        "pixel_area_true_m2": pixel_area_true_m2,
        "per_category": {
            lay.category: {"components": lay.components, "kept": lay.kept, "removed": lay.removed} for lay in layers
        },
    }
    # C-17: global labels per category band; union areas overall and per category.
    bands = []
    union = np.zeros(g10.shape, dtype=bool)
    by_category: dict[str, int] = {}
    for lay in layers:
        lut = np.zeros(lay.kept + 1, dtype=np.int32)
        for e in events:
            if e.category_code == lay.code:
                lut[e.label] = e.event_label
        band = lut[lay.labels.data]
        bands.append(GridArray(g10, band, lay.labels.valid.copy()))
        member = band > 0
        by_category[lay.category] = int(member.sum())
        union |= member
    return EventsResult(
        layers=tuple(layers),
        events=tuple(events),
        mmu_min_px=min_px,
        steps=(morph_step, *poly_steps),
        event_label_bands=tuple(bands),
        changed_px=int(union.sum()),
        changed_px_by_category=by_category,
        pixel_area_true_m2=pixel_area_true_m2,
    )
