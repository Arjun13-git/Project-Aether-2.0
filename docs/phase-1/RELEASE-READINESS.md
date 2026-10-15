# Phase 1 — release readiness (EO data foundation)

**Date:** 2026-10-15 · **Base commit:** `9a4dcee` (working tree uncommitted) · **Scope:** close the Phase 1 release items only. No Phase 2 work was done.

## A. Executive verdict

```text
READY FOR COMMIT
```

The CI-equivalent run (both workflow jobs, on a clean copy of the shippable tree) and a real-browser walkthrough of the complete Phase 1 flow were both executed and both passed. This pass found and fixed four genuine defects (§C.3). The remaining notes (§G) are documented and do not affect correctness.

## B. Implementation summary

Phase 1 is an evidence-first Earth-observation **snapshot** workbench for one AOI and one period:

AOI (C3 §2, immutable) → Earth Search `sentinel-2-c1-l2a` search → eligibility E1–E7 + duplicates → observations (datatakes) → G10/G20 UTM grid → windowed COG range reads (6 assets, exact lattice) → radiometry from per-asset `raster:bands` (C2) → SCL `mask.scl.v1` + 60 m dilation → per-observation NDVI/NDWI/MNDWI/NDBI → per-pixel temporal median with `n_valid ≥ 3` → coverage (C6 §5.2) and zonal statistics → immutable Evidence/Measurements with typed provenance → PostGIS → artifact-ID tiles → Next.js/MapLibre workspace.

| Component | Contents |
|---|---|
| `apps/api` | FastAPI and the jobs worker (`SKIP LOCKED`); content-addressed local store; baseline migration `0001` |
| `apps/web` | Workspace UI (Next.js, MapLibre) |
| Details | `ARCHITECTURE.md`, `API.md` |

## C. Verification results (final run, after all fixes)

| Check | Result | Detail |
|---|---|---|
| Unit tests | **PASS** | 128 passed (`tests/unit`) |
| Integration tests | **PASS** | 20 passed against PostGIS 18-3.6 (`tests/integration`); 1 live test deselected by default |
| Scientific invariant tests | **PASS** | 102 passed (radiometry, mask, resampling, grid, area, alignment, indices, composite I-18 pin, coverage, evidence/provenance, real-data reference ≤ 1e-6) |
| Reproducibility | **PASS** | `scripts/repro_check.py`: 6/6 persisted runs re-executed from recorded STAC JSON. 48/48 measured artifacts byte-identical, 0 statistic mismatches, display RGB identical for 5/5 linked runs (1 pre-fix run has no display link, §G) |
| Backend lint | **PASS** | `ruff check` (src, tests, migrations, scripts) and `ruff format --check` (63 files) |
| Backend type check | **PASS** | `mypy src`: no issues in 44 files |
| Frontend lint | **PASS** | `eslint` |
| Frontend type check | **PASS** | `next typegen && tsc --noEmit` |
| Frontend build | **PASS** | `next build` (Turbopack), static `/` |
| CI-equivalent run | **PASS** | All 10 `ci.yml` steps on a fresh copy of the shippable tree (`git ls-files -co --exclude-standard`): API job 13 s, web job 12 s. Licence tripwire: 87 Python + 413 web packages, 0 unknown, 0 denied |
| Browser walkthrough | **PASS** | Headless Google Chrome (system install) driven by Playwright, full flow (§E) |
| Browser console/network | **PASS** | 0 unexpected 4xx/5xx, 0 failed tiles, 0 JS exceptions. Expected: 2 × 422 from the deliberate error tests; 3 OpenFreeMap style warnings; aborted tile requests during zoom (client cancellation) |
| Live Earth Search validation | **PASS** | `pytest -m live`: 1 passed (verbatim item JSON). Live runs via the browser: discovery 3.3–3.8 s (30 items, 15 eligible observations); analysis 90.7 s cold, 5.1 s cached |
| Git hygiene | **PASS** | Nothing staged; no secrets, `.env`, caches, logs, weights or large binaries in shippable files; largest file `pnpm-lock.yaml` 152 KiB; fixtures 0.4 MB |

### C.1 CI caveat
The workflow was reproduced command by command on this machine (Python 3.13.15, uv 0.12, Node 26, pnpm 11, PostGIS in Docker). It has not run on a GitHub runner, because nothing has been pushed.

### C.2 Browser caveat
The walkthrough used a real Chrome engine rendering the production build with WebGL (SwiftShader). It was scripted for this review and is not a committed E2E suite.

### C.3 Defects found and fixed in this pass

| # | Defect | Found by | Fix |
|---|---|---|---|
| 1 | MapLibre's web worker failed to load in the production build ("Worker failed to load"). The basemap stayed blank, the map `load` event never fired, and AOI drawing was impossible | Browser walkthrough | Worker copied from `node_modules` to `public/vendor/` at dev/build (`apps/web/scripts/copy-maplibre-worker.mjs`, git-ignored output) + `maplibregl.setWorkerUrl` |
| 2 | A run whose display RGB was byte-identical to an earlier run's had no true-colour layer: content addressing reused the artifact row, and the layer lookup used `produced_by_run_id` (first producer) | Browser walkthrough (repeat run) | Typed `display_composite` provenance step with the RGB sha256; `repo.run_artifacts` resolves a run's outputs via evidence + that step (+ first-producer). Regression test added |
| 3 | `pnpm typecheck` failed on a clean checkout (`LayoutProps` not found): Next generates route types only during dev/build | Clean-copy CI run | `typecheck` = `next typegen && tsc --noEmit` |
| 4 | `alembic upgrade head` ignored `apps/api/.env`, so the documented setup failed | Service start-up | `migrations/env.py` falls back to the app `Settings` |

Also fixed:
- duplicate basemap attribution (the style already supplies it);
- tile cloud cover shown as a raw fraction ("0.261"); now "26.1 % of tile".

## D. Transient read policy (C1 §8, unchanged)

The frozen contract already defines the behaviour, and the implementation matched it. **No retry semantics were changed.** What changed is observability.

| Situation | Class | Behaviour | Result |
|---|---|---|---|
| `RasterioError`/`OSError` while opening or range-reading a window | Transport / I/O | 1 attempt + 3 retries, backoff 1 s, 2 s, 4 s; GDAL's own HTTP retries (`GDAL_HTTP_MAX_RETRY=3`, 30 s timeout) underneath | Success → normal `source_read` (same bytes, identical results; tested) |
| All attempts fail | Permanently unreadable for this run | Item **excluded** `asset_unreadable:<asset>` | Never contributes pixels; `source_read_failed` provenance step with per-attempt error class + underlying GDAL/curl message; counted in coverage evidence (`items_excluded.asset_unreadable:<asset>`) |
| Other CRS, off-lattice, header ≠ STAC, dtype mismatch | Scientific ineligibility | **Never retried** | Own reason codes (`different_utm_zone`, `grid_misaligned`, `radiometry_*`) |

Investigation of the 2026-10-13 failure:
1. **The exact error was not recoverable.** Only rasterio's wrapper `RasterioIOError("Read failed. See previous exception for details.")` had been logged. The underlying cause chain is now captured (`error_chain`).
2. **Category: transport/I-O during a window range read.** The asset opened fine, and the identical window read correctly minutes later. That is inconsistent with a malformed, corrupt or non-COG asset or with missing range support.
3. **Retry:** yes, as above.
4. **Determinism:** a retried success yields the same bytes. An exhausted failure is a recorded exclusion, so the run is explainable, but it differs from a run without the failure. Identical resubmissions return the cached run (idempotency).
5. **Coverage semantics are unchanged:** the excluded item's observation either loses that tile's pixels or drops out of `observations_usable`, and C6 §5.2 applies as written.

Tests (offline):
- transient failures then success give an identical window, with sleeps of [1, 2] s;
- exhausted retries record the GDAL/curl cause;
- lattice failures are not retried;
- an unreadable item is excluded with provenance and never used.

## E. Browser validation

Environment: production build (`pnpm build && pnpm start`), API and worker on 127.0.0.1, Chrome headless with a 1500 × 900 viewport.

| Step | Observed |
|---|---|
| Startup | Page loaded in 3.0 s; title "Aether — EO snapshot workbench"; `/api/v1/health` ok; OpenFreeMap basemap rendered |
| Error state (AOI) | Pasted a zone-crossing polygon → banner `aoi_crosses_utm_zone: AOI must lie within one UTM zone`; nothing created |
| AOI drawing | Drew a rectangle on the map → "Validate & save" → AOI persisted (EPSG:32610, 2.209 km² geodesic from `aoi_measurement` evidence) and rendered as an outline; geometry in the DB matches the drawn ring |
| Error state (period) | 10-day period → banner `period_length_out_of_range: period length 10 days is outside 30–120`; no job created |
| Discovery | 2024-06-01 → 2024-08-15: 30 items, 15 eligible observations, all `used`; tile cloud shown as "% of tile" with the "tile-level, not AOI" note |
| Snapshot | Job visible: queued → running (ingest 0 → 93 %) → succeeded. Cold run 90.7 s; repeat (cached windows) 5.1 s; outcome "Completed" |
| Layers | True colour, NDVI, n_valid, "No reliable data" and MNDWI all switched and rendered: 20 tiles each at 200, 0 errors; legends show unit, range, palette, grid, resolution, dates |
| Map interaction | Wheel zoom ×3 and drag-pan: 144 tiles at 200, 51–56 × 204 (outside raster), 0 errors |
| Evidence | Coverage "100.0 % of the AOI (pixel area)" and NDVI median 0.202 in the UI equal the API Measurements exactly; evidence IDs, method@version, limitations and the "untuned defaults" disclosure are shown |
| Provenance | Link opens the JSON chain: 30 scenes with asset hrefs; steps `catalog_search, source_read, radiometric_normalization, masking, resampling, index, compositing, display_composite, statistics`; code version recorded |
| Console/network | See §C. No CORS issues (same-origin proxy); polling only, no websockets |

## F. Scientific contract verification

| Rule | Verified by | Status |
|---|---|---|
| Indices = NDVI, NDWI, MNDWI, NDBI only | `science.INDICES`; grep finds no BSI/SAVI/NBR/EVI code | ✓ |
| B12/`swir22` never read | `REQUIRED_ASSETS = (blue, green, red, nir, swir16, scl)`; the parser drops all other assets | ✓ |
| Index first → temporal median (I-18) | Only `median_index_composite(IndexObservation…)` feeds measurements. Real-data test: band-first would differ by up to 0.081, and the engine matches the index-first reference within 1e-6 | ✓ |
| Per-band medians only for RGB display | `display_rgb_composite` → `DisplayComposite`, rejected by stats/composite functions (tested); NIR QA composite is Phase 2 | ✓ |
| Same-grid arithmetic (I-19) | `GridArray` + `require_same_grid`; mismatch tests (CRS, transform, shape, resolution, hash) | ✓ |
| Radiometry from asset metadata (C2) | Allow-list on STAC `raster:bands` + header cross-check; constants exist only in `science.py`; real COG headers verified | ✓ |
| `mask.scl.v1` unchanged | Valid {4,5,6}, dilate {3,8,9,10} by a radius-3 disk, snow 11; constant test | ✓ |
| Explicit denominators (C6) | DB CHECK + builder rule; UI shows the denominator with every % | ✓ |
| No calibrated/probabilistic claims | All thresholds labelled `untuned` in API and UI; no such wording in code/UI (only ESA's SCL class labels) | ✓ |
| Measured values have provenance | Completeness walk in integration tests + live provenance link | ✓ |
| Phase 2 refusal | Live `POST /analyses {type: change}` → `422 analysis_type_not_available`; `/health` lists change/analyst/reports as unavailable | ✓ |

## G. Known limitations

**Phase 1 limitations (by design):**
- single-period snapshots only;
- no A/B side-by-side view in the UI (API check exists);
- frozen index set;
- SCL-only masking, with its known omissions. Las Vegas: 31.5 % of pixel-observations masked in a clear summer; cause unverified;
- MNDWI/NDBI at 20 m;
- thresholds are untuned defaults;
- whole-AOI statistics only;
- no window-cache eviction;
- job status by polling.

**Future Phase 2 work (not defects):**
- change runs, deltas and `joint_valid`;
- NIR QA composite and co-registration;
- `change.index_diff.v1` and change evidence.

**External dependencies:**
- Earth Search availability (explicit `stac_unavailable`; job retries);
- Earth Search API terms of use not published (V-C1-4);
- OpenFreeMap hosted basemap (no SLA);
- the archive gap (2022, pre-2018) in `sentinel-2-c1-l2a`.

**Unresolved non-blocking concerns:**
- **CI** has not run on a GitHub runner (§C.1).
- **No committed browser E2E suite** and no frontend unit tests (§C.2).
- **OpenFreeMap style warnings:** 3 MapLibre warnings from the third-party style.
- **Pre-fix run without display link:** one dev-database run persisted before fix #2 has no `display_composite` link, so no true-colour layer. Its measured layers are fine; a re-run fixes it.
- **Pre-commit hooks / secret scanning** are not set up. ADR-023 limits are enforced by a unit test instead.

## H. Remaining blockers

None.

## I. Git state

- Commits created: **none**. Pushes: **none**. History: **unmodified**.
- HEAD = `9a4dcee` (`chore: add narrow test-fixture exception`).
- Staged: nothing.
- Tracked changes:
  - `M .gitignore` (+`.data/`);
  - `M README.md` (Phase 1 status);
  - `D apps/.gitkeep`, `D packages/.gitkeep`, `D services/.gitkeep`. These are intentional (ADR-022): `services/` and `packages/` are unused, and `apps/` now holds tracked content.
- Untracked: 163 shippable files under `.github/`, `apps/api/`, `apps/web/`, `docs/`, `infra/` (`compose.yaml`, `db-init/`, `.env.example`) and `scripts/`.
- Ignored local state present: `apps/api/.env`, `infra/.env`, `apps/api/.data/` (artifacts + window cache), `.venv/`, `node_modules/`, `.next/`, `public/vendor/`.
- `System-Design/` and `CLAUDE.md` remain local and git-ignored.

## J. Suggested commit groups (not executed)

```text
1. chore: bootstrap api and web projects, toolchain and local PostGIS
   (.gitignore, apps/api/{pyproject.toml,uv.lock,.python-version,.env.example,alembic.ini,migrations/env.py,script.py.mako},
    apps/web/{package.json,pnpm-lock.yaml,pnpm-workspace.yaml,tsconfig.json,eslint.config.mjs,next.config.ts,.gitignore,AGENTS.md,README.md},
    infra/, removed placeholders)
2. feat: add Sentinel-2 EO engine (AOI, grid, STAC eligibility, window reads, radiometry, SCL mask, indices, composites, statistics)
   (apps/api/src/aether/{science.py,domain/,geo/})
3. feat: add evidence model, persistence and Phase 1 baseline migration
   (apps/api/src/aether/{evidence/,db/,storage/,settings.py}, migrations/versions/0001_phase1_baseline.py)
4. feat: add job queue, worker and snapshot run persistence
   (apps/api/src/aether/{jobs/,services/})
5. feat: add Phase 1 API and artifact-ID map tiles
   (apps/api/src/aether/api/)
6. feat: add Phase 1 snapshot workspace
   (apps/web/src/, apps/web/scripts/copy-maplibre-worker.mjs)
7. test: add unit, integration, real-fixture and reference tests with tiny Copernicus fixtures
   (apps/api/tests/)
8. ci: add CI workflow and licence, reproducibility, demo and fixture scripts
   (.github/workflows/ci.yml, scripts/)
9. docs: document Phase 1 architecture, API, setup, limitations and release readiness
   (README.md, docs/)
```
