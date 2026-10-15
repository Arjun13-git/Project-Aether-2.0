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

## Status: Phase 1 (EO data foundation)

Phase 1 is an **evidence-first Earth-observation snapshot workbench**. For an AOI and one period it:
- discovers Sentinel-2 L2A scenes (Earth Search, `sentinel-2-c1-l2a`);
- applies deterministic radiometry, SCL cloud masking and per-observation indices (NDVI, NDWI, MNDWI, NDBI);
- builds median composites;
- reports coverage and index statistics as traceable Measurements with full provenance;
- shows the results on a map.

Phase 1 does **not** perform change detection, run an AI analyst, generate reports or use any ML model.

- Setup: [`docs/LOCAL-DEV.md`](docs/LOCAL-DEV.md)
- Architecture and scientific pipeline: [`docs/phase-1/ARCHITECTURE.md`](docs/phase-1/ARCHITECTURE.md)
- API: [`docs/phase-1/API.md`](docs/phase-1/API.md)
- Known limitations: [`docs/phase-1/KNOWN-LIMITATIONS.md`](docs/phase-1/KNOWN-LIMITATIONS.md)

```text
apps/api   Python 3.13 · FastAPI · PostGIS · rasterio/GDAL (API, worker, geo engine)
apps/web   Next.js · MapLibre (UI only)
infra      PostGIS via Docker Compose
scripts    demo, fixture fetch, licence report
```

Imagery attribution: Contains modified Copernicus Sentinel data. Basemap: OpenFreeMap © OpenMapTiles, data from OpenStreetMap.
