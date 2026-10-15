# Phase 1 — known limitations

These are preserved on purpose, not hidden. Where a limitation applies to a result, it is attached to that result's evidence as a limitation code.

## Scope (by design)
- **Single-period snapshots only.** There are no deltas, no change polygons, no changed areas and no change statistics.
  - `POST /analyses` with `type: "change"` returns `422 analysis_type_not_available`.
  - Change detection is Phase 2.
- **No AI analyst, no reports, no ML** of any kind: no detection, no segmentation, no learned cloud mask.
- **Index set is NDVI, NDWI, MNDWI and NDBI** (`indices.s2.v1`). BSI, NBR, SAVI and EVI are not implemented; they require `indices.s2.v2` and an ADR. NBR would also need the `swir22` asset, which the MVP does not read.
- **No visual A/B side-by-side view in the UI.** The API check (`GET /analyses/{a}/compare/{b}`) exists, but the workspace shows one snapshot at a time.
- **Single user, localhost, no authentication.**

## Scientific
- **Thresholds are untuned engineering defaults:**
  - `n_min` = 3 observations;
  - coverage cut-offs 0.5 / 0.8;
  - statistics support cut-off 0.5.

  They are versioned and labelled `untuned`, and none is calibrated.
- **SCL-only masking (`mask.scl.v1`):**
  - Thin cloud or haze can pass as clear.
  - Bright surfaces can be flagged as cloud.
  - Shadows beyond the 60 m dilation are missed.

  In the Las Vegas validation, 31 % of in-AOI pixel-observations in a clear summer period were masked as cloud/shadow. This is consistent with SCL bright-surface confusion and building cast shadows, but the cause is **not verified**.
- **MNDWI and NDBI are on the 20 m grid** (`index_resolution_20m`). NDBI is a weak built-up proxy, not a detector (`ndbi_not_builtup_detector`).
- **Index statistics are over the whole AOI** (pixel-centre rule). There are no sub-AOI zones in Phase 1.
- **Raster areas use one UTM areal-scale factor at the AOI centroid** (< 0.05 % variation within an AOI).
- **Archive gaps:** Earth Search `sentinel-2-c1-l2a` has almost no 2022 data and nothing before 2018. Such periods return `insufficient_observations`; they are never filled from another collection.

## External dependencies
- **Earth Search availability.** Searches fail explicitly with `stac_unavailable`. Analysis jobs retry up to 3 times with backoff.
- **Transient read failures exclude an item** (policy: C1 §8, see `RELEASE-READINESS.md` §D): 3 app-level retries (1, 2, 4 s) on top of GDAL's HTTP retries, after which the item is excluded with `asset_unreadable:<asset>` (C1 §8). This happened once in validation; the same window read fine minutes later. The exclusion is recorded in the run inputs and coverage evidence. A re-run of identical inputs returns the cached run (idempotency), so it does not retry the item.
- **Earth Search API terms of use and rate limits are not published** (licence register V-C1-4).
- **The OpenFreeMap basemap is a hosted service** with no SLA.

## Engineering
- **Window cache** (`AETHER_STORAGE_ROOT/cache`) has no eviction yet; delete it manually.
- **CI workflow (`.github/workflows/ci.yml`):**
  - It exists, and all its commands were executed successfully locally against a clean copy of the shippable tree (`RELEASE-READINESS.md` §C).
  - It has not yet run on a GitHub-hosted runner.
  - There are still no pre-commit hooks and no secret-scanning hooks. Fixture-policy limits are enforced by a unit test.
- **Job progress is polled** (2 s); there is no SSE stream.
- **Window reads dominate runtime:**

  | Mode | Measured |
  |---|---|
  | Cold reads, sequential (first run) | ~500 s for 15 observations |
  | Cold reads, 8 concurrent | 64–96 s for 13–14 observations |
  | Cached | < 1 s |

  Memory scales with grid size × observations (all per-observation index arrays are held for the median).
- **Browser walkthroughs are manual or scripted ad hoc** (headless Chrome, see `RELEASE-READINESS.md` §E). There is no committed browser E2E suite or frontend unit tests yet.
- **The OpenFreeMap style logs three MapLibre warnings** (`highway-shield…`/`road_shield_us` filter expressions). These come from the third-party style, not from Aether code.
- **Runs persisted before 2026-10-15 whose display RGB was deduplicated** have no `display_composite` step, so their true-colour layer is not listed. Measured layers are unaffected; re-running the AOI fixes it.
