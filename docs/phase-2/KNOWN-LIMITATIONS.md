# Phase 2 — known limitations and scientific caveats

These are stated on purpose. Where a limitation applies to a result, it is attached to that result's evidence as a limitation code. All Phase 1 limitations in [`../phase-1/KNOWN-LIMITATIONS.md`](../phase-1/KNOWN-LIMITATIONS.md) still apply to each period; that document's Phase 1-only scope statements are superseded by this phase.

## Scientific status
- **Thresholds are untuned.** `k = 3`, `a_NDVI = 0.15`, `a_MNDWI = 0.2`, `a_NDBI = 0.1`, `mmu_m2 = 5000`, the robust-reference and widespread-change rules, and the confidence-band rules are engineering defaults (C5 §9). They are frozen, versioned and cannot be overridden through the API (C-09). Changing one is a new algorithm version. Every change evidence record carries `thresholds_untuned`.
- **No evaluation (C-13, deferred).** No labelled reference data, no hard negatives and no accuracy, precision or recall exist. An evaluation-data ADR comes before any tuning. Results show where the frozen rules fire, not where change is verified.
- **Confidence bands** (`higher`/`moderate`/`lower`) are rule-based data-quality indicators: strength, support and registration residual. They are not probabilities.
- **Categories** are a typical-correlate label of a signed index change (C5 §6), e.g. `veg_loss` = significant NDVI decrease. They are not land-cover classes. NDBI is not a built-up detector (`ndbi_not_builtup_detector`).
- **Index differences only.** Change is the median-composite index difference between two season-matched periods. Phenology shifts, irrigation, harvest timing and illumination differences within the season-matching tolerance can produce events.

## Co-registration
- **Small AOIs cannot be verified.** Registration needs a 256 × 256 px window on the 10 m grid, so an AOI's projected extent must be ≥ 2,160 m on both axes to be always sufficient (> 2,120 m is necessary; C-15). Smaller AOIs still complete, but with the blocking `coregistration_not_verified` limitation, and every band is capped at `lower`. Preflight discloses this.
- **Residual gate is untuned.** The gate is 0.5 px (`coregistration_failed` above it), and `higher` additionally needs ≤ 0.3 px. Detected misregistration aborts change detection; nothing is resampled or corrected.
- **Raw PCC diagnostic (`coreg_pcc_error`).** C-02 interpretation, accepted 2026-11-09.
  - It is the raw `error` returned by the frozen `skimage.registration.phase_cross_correlation(A, B, upsample_factor=10)` (scikit-image 0.26, default `normalization="phase"`), recorded unchanged for provenance and debugging.
  - With that setting, skimage divides a phase-normalised correlation peak by the raw (un-normalised) image energies. The value is therefore close to 1.0 for any reflectance-scale input: identical, shifted or unrelated images alike. Both live runs recorded 1.000.
  - It is **not** a normalised registration-quality score, a confidence score, a probability or a percentage. No threshold or gate applies to it, and it is not used for confidence bands, event confidence or registration pass/fail. It is not calibrated and says nothing about registration on its own.
  - Replacing it with an informative diagnostic would be a new contract decision and a new `coreg` version.
- **Current registration assessment** rests on the frozen, untuned diagnostics:
  - the estimated shift (dy, dx) and the residual `hypot(dy, dx)`, with the 0.5 px gate (`coregistration_failed`) and the 0.3 px ceiling for `higher`;
  - the registration support (grid jointly-valid fraction and the 256 × 256 px window);
  - the NIR QA standard deviations in the window, recorded as observational texture context, not gated.
- **Registration reliability is not calibrated.** Whether these diagnostics catch real misregistration (false aborts, false passes) is for the C-13 evaluation, together with research item R-2. No calibrated registration confidence exists.
- **Low-texture scenes.** Phase correlation on near-uniform scenes can return arbitrary shifts. No texture gate exists (C-02); this is open research item R-2.

## Coverage and data
- **Joint validity.** Only pixels valid in both periods are compared. Fractions use `joint_valid_area`; insufficient AOI joint coverage (< 0.5) is an outcome, and partial coverage (< 0.8) is a blocking limitation.
- **SCL masking only.** Thin-cloud omission and bright-surface confusion of the Sentinel-2 SCL apply in each period (`scl_thin_cloud_omission`, `scl_bright_surface_confusion`).
- **Unequal observation counts** between A and B are allowed above `n_min = 3` per period and are reported, not gated (C-11).
- **MNDWI and NDBI are 20 m** quantities replicated to 10 m by nearest neighbour for event extraction (`index_resolution_20m`).

## Display and validation
- **The category palette is a display convention.** The change-layer vocabulary and the Okabe–Ito category palette are fixed (ADR-021 addendum, accepted 2026-11-09) and pinned in tests. Colours carry no scientific meaning beyond the C5 §6 category they label.
- **EPSG:4326 copies of adjacent events can overlap by slivers.** The authoritative event geometry is the native UTM, pixel-edge `change_events.gpkg`; the GeoJSON in `GET /changes` and the evidence geometry are EPSG:4326 reprojections of it. Where events of different categories share a pixel edge, the reprojected copies can intersect in tiny slivers (about 0.00005–0.0045 m² in the two live runs, against 100 m² pixels). These are a representation artifact, not event overlap. Overlap and area semantics are defined in native UTM; event and changed areas come from pixel counts. Consumers intersecting the EPSG:4326 geometries should expect these slivers, or use the GeoPackage.
- **The Δ layers show the whole analysis grid** wherever both composites are defined (NaN outside sufficiency, plan P5), including outside the AOI. Statistics and events use only `joint_valid` pixels inside the AOI; the "not compared" layer shows the excluded area.
- **Real-data validation is two runs.** They used the Davis CA and Alviso CA demo AOIs (2023 vs 2024 summer). Cross-category event overlap (C-17) has been exercised on synthetic data only; neither real run produced overlapping events.

## Dependency security
This is separate from scientific readiness.
- **Next.js** is on 16.4.5, with `eslint-config-next` 16.4.5. The advisories affecting 16.4.0 (GHSA-2464-fhr4-w9xw, GHSA-673q-mjm6-r5xr, GHSA-p3m5-59mp-jv5r) are fixed in 16.4.1 and later.
- **One moderate advisory remains: GHSA-g3hv-75wg-m249 in `minimatch`** (10.2.6, fixed in 11.0.1). It is a development dependency, reached only through the lint toolchain (`eslint-config-next` → `eslint-plugin-import` / `typescript-eslint` → `@typescript-eslint/parser` → `@typescript-eslint/typescript-estree` → `minimatch`). It is not part of the built application or its runtime.
- It is not force-overridden: `@typescript-eslint/typescript-estree` declares `minimatch@^10.2.2`, so a fix needs a major-version override outside that range. It is to be resolved by an upstream lint-toolchain update.
- `pnpm audit` therefore reports one moderate finding. This is not a security review of the project.

## Scope (by design)
- No VHR imagery, object or building detection, learned change models, segmentation, Sentinel-1, Dynamic World or other land-cover products.
- No AI analyst (Phase 3) and no reports (Phase 4).
- Single user, localhost, no authentication.
