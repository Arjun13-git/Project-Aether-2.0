"""Earth Search STAC search (C1 §4). Single provider and collection; failures are explicit."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from aether import science
from aether.domain.errors import AetherError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchResult:
    api_url: str
    collection: str
    request: dict[str, Any]
    items: list[dict[str, Any]]  # verbatim STAC item JSON, sorted by (datetime, id)
    number_matched: int | None


class StacSearcher(Protocol):
    api_url: str

    def search(self, geometry: dict[str, Any], start: date, end: date, max_tile_cloud: float) -> SearchResult: ...


def build_request(collection: str, geometry: dict[str, Any], start: date, end: date, max_tile_cloud: float) -> dict[str, Any]:
    return {
        "collections": [collection],
        "intersects": geometry,
        "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59.999Z",
        "query": {"eo:cloud_cover": {"lt": max_tile_cloud}},
        "limit": 100,
    }


def sort_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda it: (it.get("properties", {}).get("datetime") or "", it.get("id", "")))


class EarthSearchClient:
    """POST /search via pystac-client, paginated to exhaustion with a 500-item cap."""

    def __init__(self, api_url: str, collection: str = science.STAC_COLLECTION, timeout_s: float = 30.0) -> None:
        if collection != science.STAC_COLLECTION:
            raise AetherError("wrong_collection", f"MVP uses only {science.STAC_COLLECTION}", http_status=500)
        self.api_url = api_url
        self.collection = collection
        self.timeout_s = timeout_s

    def search(self, geometry: dict[str, Any], start: date, end: date, max_tile_cloud: float) -> SearchResult:
        from pystac_client import Client
        from pystac_client.stac_api_io import StacApiIO

        request = build_request(self.collection, geometry, start, end, max_tile_cloud)
        log.info("stac.search start api=%s period=%s/%s max_tile_cloud=%s", self.api_url, start, end, max_tile_cloud)
        try:
            client = Client.open(self.api_url, stac_io=StacApiIO(timeout=self.timeout_s))
            search = client.search(method="POST", max_items=science.MAX_ITEMS_PER_PERIOD + 1, **request)
            # Raw dicts, NOT pystac Items: pystac migrates items on load (e.g. proj:epsg → proj:code,
            # stac_version 1.0 → 1.1), which would break the verbatim-JSON rule of C1 §8.
            items = list(search.items_as_dicts())
            matched = search.matched()
        except Exception as exc:
            log.warning("stac.search failed: %s", exc)
            raise AetherError(
                "stac_unavailable",
                f"Earth Search request failed: {type(exc).__name__}",
                http_status=503,
                retryable=True,
            ) from exc
        if len(items) > science.MAX_ITEMS_PER_PERIOD:
            raise AetherError("too_many_items", "more than 500 items in the period; shorten the period")
        log.info("stac.search done items=%d matched=%s", len(items), matched)
        return SearchResult(self.api_url, self.collection, request, sort_items(items), matched)
