# Test fixtures (ADR-023)

Policy: `System-Design/16-decisions/ADR-023-test-fixture-policy.md` (summary below).

- Only `tiny/` may hold rasters, and only `.tif` files (`.gitignore`: `!apps/api/tests/fixtures/tiny/**/*.tif`).
- Each committed file is at most 512 KiB, and real-data rasters are at most 256 KiB. The whole tree is at most 20 MiB.
  `tests/unit/test_evidence_provenance_policy.py` enforces these limits.
- Synthetic fixtures come first. Most suites build their inputs in code (`tests/factories.py`) and commit nothing.
- Every real Sentinel-2 window has a `<name>.provenance.json` sidecar: source, item, asset, href, window, retrieval date, licence and attribution.
- Never commit full scenes, `.SAFE` products, JP2, imagery that is not C1 or is VHR, datasets without redistribution rights, or model weights. Git LFS is not used.

## Index of non-synthetic fixtures

| File | Source | Purpose |
|---|---|---|
| `tiny/sentinel2/*_32px.tif` + `.provenance.json` | Earth Search `sentinel-2-c1-l2a`, item `S2A_T10SFH_20240602T185554_L2A` (red, swir16, scl) | Real COG header and radiometric metadata semantics (C2 §3, V-C1-2) |
| `tiny/sentinel2/*_640m.tif` + `.provenance.json` | 4 items of tile 10SEH (2024-06-07, -17, -27, 07-07), all 6 assets, a 640 m square aligned to the 20 m lattice | Engine vs independent reference (`tests/reference/`) on real data, within 1e-6 |
| `tiny/stac/*.json` | Verbatim Earth Search STAC item JSON | Eligibility parsing on real metadata (C1 §5) |

Attribution: "Contains modified Copernicus Sentinel data 2024". The total tree is about 0.4 MB.

The real fixtures are produced by `scripts/fixtures/fetch_s2_window.py`, which applies the C1 href allow-list and writes the sidecars.
