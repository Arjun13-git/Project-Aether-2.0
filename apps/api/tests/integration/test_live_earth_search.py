"""Live checks against Earth Search (``-m live``; excluded from the default run)."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from aether import science
from aether.geo.stac.client import EarthSearchClient
from aether.geo.stac.eligibility import apply_eligibility, parse_item
from tests.factories import AOI_GEOJSON

pytestmark = pytest.mark.live
API = "https://earth-search.aws.element84.com/v1"


def test_search_returns_verbatim_item_json():
    """C1 §8: persisted STAC JSON must be exactly what the API serves (no client-side migration)."""
    res = EarthSearchClient(API).search(AOI_GEOJSON, date(2024, 6, 1), date(2024, 6, 20), 80.0)
    assert res.items, "expected C1 items for summer 2024"
    first = res.items[0]
    raw = httpx.get(f"{API}/collections/{science.STAC_COLLECTION}/items/{first['id']}", timeout=30).json()
    for key in ("stac_version", "stac_extensions", "properties", "assets"):
        assert first[key] == raw[key], key
    scenes = [parse_item(i) for i in res.items]
    apply_eligibility(scenes, 32610)
    assert any(s.status == "used" for s in scenes)
