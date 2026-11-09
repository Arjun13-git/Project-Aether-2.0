"""C-08 / ADR-021 addendum: the change-layer display vocabulary and palette are pinned as implemented."""

from __future__ import annotations

from aether import science
from aether.api.tiles import CATEGORY_PALETTE, DELTA_RANGE, RENDERINGS, renderings_for
from aether.geo.change.rules import CATEGORIES

# ADR-021 addendum (C-08): Okabe–Ito colour-blind-safe palette, C5 §6 codes; code 0 (none) is transparent.
PINNED_PALETTE = {
    1: ("water_increase", "#0072B2"),
    2: ("water_decrease", "#56B4E9"),
    3: ("built_bare_increase", "#E69F00"),
    4: ("veg_loss", "#D55E00"),
    5: ("veg_gain", "#009E73"),
    6: ("other_significant", "#CC79A7"),
}


def test_category_palette_is_the_c5_vocabulary_with_the_pinned_colours():
    assert CATEGORY_PALETTE == PINNED_PALETTE
    assert {code: name for code, (name, _) in CATEGORY_PALETTE.items()} == CATEGORIES
    legend = RENDERINGS["category"].legend
    assert RENDERINGS["category"].categorical
    assert legend["classes"] == [{"code": c, "category": n, "colour": col} for c, (n, col) in PINNED_PALETTE.items()]
    assert legend["note"] == "typical-correlate label of an index change, not land-cover truth"


def test_delta_layers_are_diverging_centred_on_zero_with_the_b_minus_a_legend():
    assert DELTA_RANGE == (-0.5, 0.5)
    expected_cmaps = {"ndvi": "brbg", "mndwi": "rdbu", "ndbi": "puor"}
    assert set(science.CHANGE_INDICES) == set(expected_cmaps)
    for name, cmap in expected_cmaps.items():
        r = RENDERINGS[f"delta:{name}"]
        assert (r.rescale, r.colormap, r.categorical) == (DELTA_RANGE, cmap, False)
        assert r.legend["centre"] == 0 and r.legend["note"] == "Δ = B − A · positive = index increased"
    assert "delta:ndwi" not in RENDERINGS  # no NDWI delta (C-06)


def test_joint_valid_is_the_not_compared_mask_and_z_and_qa_are_not_map_layers():
    assert [r.name for r in renderings_for("joint_valid", ["joint_valid"])] == ["not_compared"]
    assert RENDERINGS["not_compared"].legend["colour"] == "#7f7f7f"
    assert renderings_for("z", ["z_ndvi"]) == []  # C-08: z stays a provenance artifact
    assert renderings_for("composite", ["nir_qa"]) == []  # co-registration input only (C4 §3)
    assert renderings_for("event_label", [f"event_label_{n}" for n in CATEGORIES.values()]) == []
