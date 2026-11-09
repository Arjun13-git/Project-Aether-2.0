# Phase 2 — two-period change analysis

Status as of 2026-11-09. The API is in [`API.md`](API.md) and the limitations are in [`KNOWN-LIMITATIONS.md`](KNOWN-LIMITATIONS.md). Phase 1 (single-period snapshots) is frozen and unchanged; see [`../phase-1/`](../phase-1/).

## Status at a glance

| | |
|---|---|
| **Implemented** | Real Sentinel-2 L2A (Earth Search `sentinel-2-c1-l2a`) A/B change analysis, `change.index_diff.v1`: two periods per run, joint coverage, co-registration check, Δ = B − A for NDVI/MNDWI/NDBI, significance, change categories, change events with evidence, provenance, a GeoPackage, map layers and an inspection workspace |
| **Not scientifically calibrated** | Every change threshold and event rule (`k`, `a_NDVI`, `a_MNDWI`, `a_NDBI`, `mmu_m2`, robust-reference and widespread-change rules, confidence-band rules) is an **untuned engineering default**. Every change evidence record carries `thresholds_untuned`. No accuracy, precision or recall is claimed |
| **Deferred** | The scientific evaluation and any tuning (C-13): evaluation-data ADR, labels, hard negatives, splits and metrics. Nothing in this phase is evaluated against labelled change |
| **Not included** | VHR imagery or detection, learned or ML change models, segmentation (e.g. SAM), Sentinel-1, Dynamic World or other land-cover products, an AI analyst, reports, authentication |

The system runs locally (single user, loopback only). It is not production-ready.

## Workflow

1. **AOI.** An immutable AOI, validated as in Phase 1. The analysis grid is the AOI's UTM zone on the native Sentinel-2 lattice (10 m / 20 m).
2. **Periods A and B.** Each period is 30–120 days long. A must end before B starts, and the period midpoints must be within 30 days of each other in the year (season-matched). Violations are `422` with `periods_invalid`, `period_length_out_of_range`, `period_before_archive` or `periods_not_season_matched`.
3. **Preflight** (`POST /analyses/preflight`, metadata only). Pair rules, catalogue items and STAC-level eligible observations per period (`n_min = 3`), the grid, and whether the grid is large enough for the 256 × 256 px co-registration window (C-15). Preflight does not read pixels: joint coverage and actual registration support are decided in the job.
4. **Submission** (`POST /analyses` with `type: "change"`). Only `aoi_id`, `type`, `period_a` and `period_b` are accepted; there are no threshold overrides (C-09). Identical requests return the existing run (`200`, `cached: true`), keyed by AOI, both periods, collection, frozen parameters and the change algorithm-version map (C-10).
5. **Processing** (one job, self-contained, C-07). Each period goes through the Phase 1 engine (radiometry, SCL mask, per-observation indices, median index composites with `n_valid`), then joint validity and coverage, co-registration, Δ, significance, categories, morphology, 8-connected events, polygons, evidence and the GeoPackage. Everything is persisted in one transaction.
6. **Outcome.** Evaluated in the fixed order of C-03:

   | Outcome | Meaning |
   |---|---|
   | `insufficient_observations` | Fewer than 3 usable observations in Period A or B |
   | `insufficient_coverage` | AOI joint coverage below 0.5 |
   | `coregistration_failed` | Registration residual above 0.5 px; change detection was not run |
   | `completed` | Change products are available (possibly with zero events) |

   These are scientific results, not errors. A job that fails (data access, infrastructure) has status `failed` and no outcome; the UI shows the two differently. If registration is **not assessable** (grid support or window too small), the run continues with the blocking limitation `coregistration_not_verified`, and every event's band is capped at `lower`.

## Results

- **Layers** (`GET /analyses/{id}/layers`, each with its period):
  - Period A and Period B: true colour (display only), index composites, `n_valid`, no-reliable-data;
  - Δ NDVI/MNDWI/NDBI (diverging, centred on 0, "Δ = B − A · positive = index increased");
  - the change category (fixed palette; a typical-correlate label, not land cover);
  - not compared (the `joint_valid` mask).

  z layers, the NIR QA composite and the event-label raster are provenance artifacts, not map layers.
- **Events** (`GET /changes`). One GeoJSON feature per event, with a global `event_label` 1..N. Events of different categories may overlap and are never merged (C-17). Each event carries:
  - category, stored confidence band (`higher`/`moderate`/`lower`), mode (`robust` or `absolute_only`) and boundary flag;
  - measurements: area, pixel count, median Δ per index, strength, support A/B;
  - per-index sign lists in `-`, `0`, `+` order with positive/negative pixel counts (C-18);
  - limitations, and the authoritative `change_events.gpkg` artifact (UTM, pixel-edge geometry, FID = `event_label`). The GeoJSON is an EPSG:4326 copy.
- **Run evidence:**
  - coverage for A and B (named denominators);
  - per-period index statistics;
  - Δ statistics for NDVI/MNDWI/NDBI over `joint_valid` pixels (C-06);
  - the co-registration check: status, shift, residual, support window, NIR QA σ and the raw phase-correlation error. The raw error is a diagnostic only, not a quality or confidence score (see the limitations);
  - the change summary: changed area and fractions over `joint_valid_area`, per-category areas (unions; they may sum to more than the total where categories overlap), and event counts.
- **Provenance** (`GET /evidence/{id}/provenance`): versions, processing steps per period, source scenes per period, and cited artifacts. Run inputs (`GET /analyses/{id}` → `inputs[]`) carry the period recorded for each item.

Confidence bands are rule-based data-quality indicators, not calibrated probabilities. Category names describe a typical correlate of an index change, not verified land cover.

## Validation performed

- **Synthetic worlds** (deterministic, programmatic; `apps/api/tests/change_fixtures.py`) cover every outcome, registration not verified, unequal observation counts, cross-category overlap, C-12 robust-reference cases and C-18 sign lists. They run through the real worker, persistence, API and browser.
- **Two live A/B runs** through the production path, recorded 2026-11-09. The first:

  | Item | Value |
  |---|---|
  | AOI | Davis CA demo AOI, bbox −121.80, 38.52 → −121.77, 38.545 |
  | Periods | A 2023-06-01 → 2023-08-15, B 2024-06-01 → 2024-08-15 |
  | Observations | 15 usable per period (30 items each) |
  | Joint coverage | 0.997 |
  | Co-registration | `ok`, residual 0.00 px |
  | Outcome | `completed`, robust mode |
  | Events | 28: 10 `higher`, 18 `moderate` |
  | Changed area | 15.1 % of `joint_valid_area` |

  A browser walkthrough of that run had no console or network errors.
- **Second live run:** Alviso CA salt ponds, same periods. 15 vs 14 usable observations (unequal counts, C-11), joint coverage 0.993, co-registration `ok`, outcome `completed`, 20 events (15 `higher`, 5 `moderate`).
- These are two AOIs and one season pair: they show the pipeline works end to end on real data, not that its results are accurate.
- **Real overlap not observed.** No two events overlapped in either live run, so cross-category overlap is validated on synthetic data only.

## Reproducibility

- `scripts/repro_check.py` re-executes **snapshot** runs and compares measured artifacts and statistics. It ignores change runs.
- `scripts/repro_change_check.py` re-executes **change** runs from their recorded catalogue response, in a read-only database transaction, into a throw-away store. It first requires the recorded identity (parameters, versions, change key) to equal what the current code derives, then compares exactly:
  - processing steps, inputs and outcome;
  - every artifact (GeoPackage included) and the bytes of the persisted ones;
  - every evidence record and measurement;
  - the change events.

  Random identifiers, timestamps, timings, code version, environment, and whether a window came from the cache are not compared. Both live runs reproduce with every comparison identical.
