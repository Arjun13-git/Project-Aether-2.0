# Project Aether 2.0

**Evidence-first Earth observation and geospatial change-analysis platform.**

Project Aether 2.0 is a clean rebuild of the original Aether prototype. It focuses on real Earth-observation data, deterministic geospatial analysis, reproducible evidence, and an optional AI analyst that explains measured results rather than inventing them.

## Core workflow

1. Define an area of interest (AOI).
2. Search real satellite imagery using STAC.
3. Inspect scene metadata and quality.
4. Select comparable observations.
5. Apply deterministic preprocessing and cloud masking.
6. Compute spectral indices and geospatial measurements.
7. Detect and represent changes as evidence.
8. Persist provenance and results.
9. Visualize findings on an interactive map.
10. Optionally ask Gemini to explain evidence through controlled tools.

## Repository status

This repository starts from zero. The original Aether 1.x codebase is intentionally separate and archived.

`System-Design/` contains the local engineering blueprint and is gitignored.

## Status: snapshot and two-period change analysis

**Snapshot analysis (Phase 1, frozen).** For an AOI and one period it:
- discovers Sentinel-2 L2A scenes (Earth Search, `sentinel-2-c1-l2a`);
- applies deterministic radiometry, SCL cloud masking and per-observation indices (NDVI, NDWI, MNDWI, NDBI);
- builds median composites;
- reports coverage and index statistics as traceable Measurements with full provenance;
- shows the results on a map.

**Change analysis (`change.index_diff.v1`).** For an AOI and two season-matched periods A and B it:
- runs the snapshot engine for each period inside one self-contained run;
- measures AOI joint coverage and checks co-registration (phase correlation on NIR QA composites; residual gate 0.5 px);
- computes Δ = B − A for NDVI, MNDWI and NDBI, classifies significant change, and extracts change events (8-connected, pixel-edge polygons, authoritative GeoPackage);
- records per-event measurements, sign counts and a rule-based confidence band (`higher`/`moderate`/`lower`), with fractions over `joint_valid_area`;
- reports a scientific outcome (`completed`, `insufficient_observations`, `insufficient_coverage`, `coregistration_failed`) distinct from infrastructure failure;
- shows Period A/B layers, Δ layers, categories, the not-compared mask and inspectable events in the change workspace.

Validation: synthetic change worlds cover every outcome in tests. Two live Earth Search A/B runs have been recorded (Davis CA and Alviso CA demo AOIs, 2023-06-01→08-15 vs 2024-06-01→08-15, both `completed`). `scripts/repro_check.py` checks snapshot runs; `scripts/repro_change_check.py` re-executes change runs from their recorded catalogue response and compares artifacts, GeoPackage, evidence and events.

Limitations:
- All change thresholds (`k`, `a_*`, MMU, confidence-band rules) are **untuned engineering defaults**, not calibrated values, and cannot be overridden through the API.
- The scientific evaluation of change detection (evaluation data and labels, C-13) is **deferred**. No accuracy is claimed.
- AOIs narrower than about 2.16 km on either UTM axis cannot have co-registration verified. Their events carry a blocking `coregistration_not_verified` limitation and a `lower` band.
- The change-category display palette is provisional.
- The phase-correlation error (`coreg_pcc_error`) is recorded raw as a diagnostic only. It is not a registration-quality or confidence score and gates nothing; registration reliability is not calibrated (C-13). See the Phase 2 limitations.

The project does **not** run an AI analyst, generate reports or use any ML model.

- Setup: [`docs/LOCAL-DEV.md`](docs/LOCAL-DEV.md)
- Change analysis: [`docs/phase-2/README.md`](docs/phase-2/README.md) · API: [`docs/phase-2/API.md`](docs/phase-2/API.md) · limitations and scientific caveats: [`docs/phase-2/KNOWN-LIMITATIONS.md`](docs/phase-2/KNOWN-LIMITATIONS.md)
- Phase 1 architecture and scientific pipeline: [`docs/phase-1/ARCHITECTURE.md`](docs/phase-1/ARCHITECTURE.md)
- Phase 1 API: [`docs/phase-1/API.md`](docs/phase-1/API.md)
- Phase 1 known limitations: [`docs/phase-1/KNOWN-LIMITATIONS.md`](docs/phase-1/KNOWN-LIMITATIONS.md)

```text
apps/api   Python 3.13 · FastAPI · PostGIS · rasterio/GDAL (API, worker, geo engine)
apps/web   Next.js · MapLibre (UI only)
infra      PostGIS via Docker Compose
scripts    demo, fixture fetch, licence report
```

Imagery attribution: Contains modified Copernicus Sentinel data. Basemap: OpenFreeMap © OpenMapTiles, data from OpenStreetMap.
