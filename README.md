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

## First milestone

Phase 1 proves the geospatial foundation before adding advanced ML or the AI analyst.
