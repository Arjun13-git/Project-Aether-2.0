# Phase 1 — architecture and scientific pipeline

Aether Phase 1 is an **evidence-first Earth-observation snapshot workbench**. For one AOI and one period it:
1. discovers Sentinel-2 L2A scenes;
2. processes them deterministically into per-period index composites;
3. reports coverage and index statistics as traceable Measurements.

It does **not** detect change. Change analysis is Phase 2, the AI analyst Phase 3 and reports Phase 4.

The normative contracts live in the local `System-Design/` directory (C1–C8, ADR-011…023). This document describes what is implemented and where.

## Processes

```text
Browser ── Next.js (apps/web, UI only, proxies /api) ──HTTP──► FastAPI (apps/api, 127.0.0.1:8000)
                                                               ├─ /api/v1 routers
                                                               └─ /api/v1/tiles (rio-tiler, artifact ID only)
                                                                      │ jobs table
                                                                      ▼
                                                   worker (python -m aether.jobs.worker)
                                                                      │
          PostgreSQL + PostGIS (Docker) ◄──────────────────────────────┼──────► Earth Search STAC + public COGs
          Local filesystem object store (AETHER_STORAGE_ROOT) ◄────────┘        (HTTP range reads only)
```

| Package | Responsibility |
|---|---|
| `aether.science` | Every versioned scientific constant. Untuned values are labelled |
| `aether.domain` | Errors (reason codes), registries (quantities, limitations), period rules. No I/O |
| `aether.geo` | Pure engine: AOI, grid, STAC client and eligibility, window reads, radiometry, resampling, SCL mask, indices, composites, statistics, the snapshot pipeline, COG writer |
| `aether.evidence` | Evidence/Measurement builders with C7 validation; typed provenance steps; environment allow-list |
| `aether.storage` | `ObjectStore` port + `LocalFilesystemStore` (content-addressed) |
| `aether.db` | Engine, explicit-SQL repositories |
| `aether.jobs` | `jobs` table queue (`SKIP LOCKED`), worker, heartbeat, reaper |
| `aether.services` | AOI creation, catalogue search, analysis submission/read models, snapshot run persistence, provenance view |
| `aether.api` | FastAPI app, schemas, tiles |

`aether.geo` imports neither the database nor FastAPI.

## Scientific pipeline (as implemented)

```text
AOI (GeoJSON, EPSG:4326)
 → admissibility C3 §2 + normalisation (orient, round 1e-9°, recorded repairs)    geo/aoi.py
 → analysis grid: AOI UTM zone, 20 m-snapped bounds + 200 m buffer, G10/G20       geo/grid.py
 → Earth Search POST /search, sentinel-2-c1-l2a, tile cloud < 80 (pre-filter)     geo/stac/client.py
 → eligibility E1–E7, duplicates, observations = datatakes                         geo/stac/eligibility.py
 → per item: 6 asset windows (blue, green, red, nir @10 m; swir16, scl @20 m)     geo/io/windows.py
     exact lattice check (no resampling at read), header vs STAC cross-check
 → mosaic items of one datatake (first valid by item id)                           geo/snapshot.py
 → radiometry: DN 0 / 65535 invalid; ρ = DN × 0.0001 − 0.1 (float32, unclipped)   geo/radiometry.py
 → SCL mask.scl.v1 on G20: valid {4,5,6}; dilate {3,8,9,10} by a radius-3 disk     geo/masks.py
   (60 m); R1 nearest to G10
 → per-observation indices: NDVI, NDWI (G10); MNDWI, NDBI (G20, via R2 block mean) geo/indices.py
 → per-period composite = median over observations of the per-observation index   geo/composite.py
   (n_valid per pixel; value only where n_valid ≥ 3)
 → coverage (C6 §5.2 snapshot outcome) + zonal statistics (C4 §5)                  geo/stats.py
 → COG artifacts (content-addressed) + Evidence/Measurements, one DB transaction   services/snapshot_run.py
```

### Raster grid semantics
- All pixel arithmetic happens on one of two grids that share a `grid_hash`:
  - **G10**: 10 m;
  - **G20**: 20 m, which nests G10 exactly.
- Every raster in the engine is a `GridArray` (data + explicit validity mask + grid). Operations combining rasters call `require_same_grid`, so mixing CRS, transform, shape or resolution raises `grid_mismatch` (I-19).
- The only cross-resolution operations are:
  - **R1**: SCL 20 → 10 m, nearest;
  - **R2**: green/NIR 10 → 20 m, 2 × 2 mean, valid only if all four children are valid.
- Reflectance is never reprojected or interpolated. Source windows must sit exactly on the grid lattice, or the item is excluded with `grid_misaligned`.
- Raster areas are `n_pixels × pixel_area_true`, where `pixel_area_true = res² / UTM areal scale` at the AOI centroid. The AOI area itself is geodesic (WGS84).
- AOI membership uses the pixel-centre rule.

### Cloud masking (`mask.scl.v1`)
| SCL | Treatment | Coverage reason |
|---|---|---|
| 4, 5, 6 | valid (unless inside the dilation) | `clear` |
| 3, 8, 9, 10 | invalid, dilated by a radius-3 disk (60 m) | `cloud_shadow` |
| 2 (cast shadows) | invalid | `cloud_shadow` |
| 11 | invalid | `snow_ice` |
| 0 | invalid | `nodata` |
| 1, 7 | invalid | `other_invalid` |

Masked fractions are reported per reason, with the denominator `aoi_pixel_observations`. Known SCL limitations (thin-cloud omission, bright-surface confusion) are attached to every mask-derived evidence.

### Indices (`indices.s2.v1`, frozen MVP set)
| Index | Formula | Bands (S2A centre λ) | Grid |
|---|---|---|---|
| NDVI | (nir − red) / (nir + red) | B08 833 nm, B04 665 nm | G10 |
| NDWI (McFeeters) | (green − nir) / (green + nir) | B03 560 nm, B08 | G10 |
| MNDWI | (green₂₀ − swir16) / (green₂₀ + swir16) | B03 (2 × 2 mean), B11 1614 nm | G20 |
| NDBI | (swir16 − nir₂₀) / (swir16 + nir₂₀) | B11, B08 (2 × 2 mean) | G20 |

A value is **undefined** if:
- either input is invalid (radiometry or mask);
- `a + b ≤ 1e-4`;
- `|x| > 1`.

Undefined values are NaN with `valid = False`. Statistics select samples with the mask, so NaN never reaches a number. In COG artifacts, undefined is the declared nodata (NaN). BSI, NBR, SAVI and EVI are not part of the frozen set; they need `indices.s2.v2` and an ADR (owner decision, 2026-10-13).

### Composite semantics (I-18)
For each index:
1. compute the index **per observation**;
2. take the **per-pixel median** over observations where it is defined.

`n_valid` counts those observations. Pixels with `n_valid < 3` have no composite value. They are never filled.

The measured composite function accepts only `IndexObservation` objects, and those can only be produced from two reflectances of the same observation. Per-band reflectance medians exist only as a `DisplayComposite` (the RGB map layer), which no measurement function accepts. A unit test pins a case where the two strategies differ.

### Coverage and outcomes (C6 §5.2)
| Condition (in order) | Outcome | Index statistics |
|---|---|---|
| < 3 usable observations (≥ 1 defined in-AOI NDVI pixel) | `insufficient_observations` | unavailable |
| `coverage_a` < 0.50 | `insufficient_coverage` | unavailable |
| 0.50 ≤ `coverage_a` < 0.80 | `completed` + blocking `partial_coverage` | produced |
| `coverage_a` ≥ 0.80 | `completed` | produced |

- `coverage_a` = `valid_area_a / aoi_grid_area`, where valid means NDVI `n_valid ≥ 3`.
- Every fraction Measurement names its denominator and links to the denominator Measurement.
- A statistic whose support is below 50 % of its region is `unavailable — insufficient_support`, never 0.
- All thresholds (`n_min` = 3, 0.5/0.8, support 0.5) are **untuned, versioned defaults**. The API returns them with `calibration: "untuned"`.

## Evidence and provenance
- **Evidence** is an immutable bundle with these kinds:
  - `aoi_measurement`;
  - `catalog_query`;
  - `scene_metadata`;
  - `coverage_statistic`;
  - `index_statistic`.
- **Facts** are non-numeric only; this is enforced.
- **Measurements** are typed numbers with unit, status, denominator, method@version and `n_samples`.
- The DB enforces:
  - `value IS NULL ⇔ unavailable`;
  - a reason whenever a value is unavailable;
  - a denominator for every fraction;
  - no NaN or ±∞;
  - immutability, by trigger.
- **ProcessingRun** records:
  - parameters + hash;
  - algorithm versions;
  - grid hash;
  - git SHA (`-dirty` when the tree has changes);
  - an allow-listed environment;
  - timings;
  - typed steps: `catalog_search`, `source_read` (href, checksum, window), `source_read_failed` (read-time exclusion + transport attempts), `radiometric_normalization` (scale/offset/nodata + invalid counts), `masking`, `resampling`, `index`, `compositing`, `statistics`.
- `GET /api/v1/evidence/{id}/provenance` walks evidence → processing run → analysis inputs (used/excluded + reason) → scenes → asset hrefs/checksums, and lists artifacts.

## Jobs
- One `analysis_run` job per snapshot.
- The worker claims with `FOR UPDATE SKIP LOCKED`, heartbeats every 10 s, and checks cancellation and the 20-minute timeout at every progress report.
- Retryable errors (Earth Search unavailable) requeue with backoff, up to 3 attempts. Contract and invariant errors are fatal.
- A reaper requeues jobs with a heartbeat older than 120 s.
- Results are written in one final transaction. A failure leaves no evidence.

## Map delivery
- `GET /api/v1/tiles/{artifact_id}/{z}/{x}/{y}.png?render_name=…` reads the COG through rio-tiler, with fixed rendering per product:
  - RGB: 0–0.3;
  - indices: −1…1 with colour-blind-safe diverging palettes;
  - `n_valid`: 0–30 viridis;
  - "no reliable data": `n_valid < 3`.
- TiTiler's `TilerFactory` is not mounted, because its public `url` / `rescale` / `colormap` parameters would bypass the fixed rendering. This is recorded in ADR-021's implementation note.
- The basemap is OpenFreeMap (Positron).
- MapLibre's web worker is served from `/vendor/maplibre-gl-worker.mjs`. `scripts/copy-maplibre-worker.mjs` copies it at dev/build time, because the bundled default worker URL does not resolve under Turbopack.
- A run's map layers are resolved by `repo.run_artifacts`:
  - artifacts cited by its evidence;
  - its `display_composite` provenance step (which carries the RGB sha256);
  - artifacts it produced first.

  Content addressing deduplicates identical rasters across runs, so `produced_by_run_id` alone cannot identify a run's outputs.
