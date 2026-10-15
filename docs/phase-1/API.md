# Phase 1 API (`/api/v1`)

The full schema is at `http://127.0.0.1:8000/docs`. All errors use one body:

```json
{"error": {"code": "<reason code>", "message": "…", "details": {}, "request_id": "…"}}
```

Codes are the contract reason codes (e.g. `aoi_crosses_utm_zone`, `period_length_out_of_range`, `analysis_type_not_available`, `stac_unavailable`).

| Method & path | Purpose |
|---|---|
| `GET /health` | Versions, capabilities, and what is **not** available (change, analyst, reports) |
| `GET/POST /projects`, `GET/DELETE /projects/{id}` | Projects (delete cascades, including immutable evidence) |
| `POST /projects/{id}/aois` | Validate (C3 §2) and store an immutable AOI; writes `aoi_measurement` evidence (geodesic area) |
| `GET /projects/{id}/aois`, `GET /aois/{id}` | Read AOIs |
| `POST /catalog/search` | Synchronous Earth Search query. Returns each scene's eligibility (`used`/`excluded` + reason), its `scene_metadata` evidence and the `catalog_query` evidence |
| `POST /analyses/preflight` | Period rules, grid size and eligible observation count without running anything |
| `POST /analyses` | Submit a **snapshot**. `202` new run, or `200` the existing run for identical inputs (idempotent). `type: "change"` → `422 analysis_type_not_available` |
| `GET /projects/{id}/analyses`, `GET /analyses/{id}` | Run status, outcome, evidence IDs, inputs (used/excluded), parameters with calibration status |
| `GET /analyses/{id}/layers` | Map layers: artifact ID, fixed rendering, legend, grid |
| `GET /analyses/{a}/compare/{b}` | Whether two snapshots may be shown side by side: same AOI/grid plus the C4 §1 pair rules. Computes nothing |
| `GET /jobs/{id}`, `POST /jobs/{id}/cancel` | Job status (stage, progress, structured error) / cancel |
| `GET /evidence/{ev_id}` | Evidence + Measurements + limitation texts |
| `GET /evidence/{ev_id}/provenance` | Processing run (parameters, versions, code version, environment, typed steps, timings), scenes with asset hrefs/checksums, artifacts |
| `GET /tiles/{artifact_id}/{z}/{x}/{y}.png?render_name=` | Display tiles by artifact ID (fixed rendering) |

## Example

```bash
API=http://127.0.0.1:8000/api/v1
P=$(curl -s -XPOST $API/projects -H 'content-type: application/json' -d '{"name":"demo"}' | jq -r .id)
AOI=$(curl -s -XPOST $API/projects/$P/aois -H 'content-type: application/json' -d '{"name":"Davis fields",
  "geometry":{"type":"Polygon","coordinates":[[[-121.80,38.52],[-121.77,38.52],[-121.77,38.545],[-121.80,38.545],[-121.80,38.52]]]}}' | jq -r .id)
curl -s -XPOST $API/catalog/search -H 'content-type: application/json' \
  -d "{\"aoi_id\":\"$AOI\",\"period\":{\"start\":\"2024-06-01\",\"end\":\"2024-08-15\"}}" | jq '.scenes[] | {item_id,status,reason_code}'
RUN=$(curl -s -XPOST $API/analyses -H 'content-type: application/json' \
  -d "{\"aoi_id\":\"$AOI\",\"type\":\"snapshot\",\"period_a\":{\"start\":\"2024-06-01\",\"end\":\"2024-08-15\"}}")
curl -s $API/jobs/$(echo $RUN | jq -r .job_id) | jq '{status,stage,progress,error}'
curl -s $API/analyses/$(echo $RUN | jq -r .analysis_run_id) | jq '{status,outcome,evidence}'
```

## Reading numbers correctly
- Every number is a Measurement with `unit` ∈ {m2, count, 1, deg}.
- A `fraction` always carries `denominator_quantity` and `denominator_measurement_id`. For example, `coverage_a` is relative to `aoi_grid_area`, not the geodesic `aoi_area`.
- `status: "unavailable"` means `value: null` plus an `unavailable_reason`. It is never 0.
- Scene cloud cover is `tile_cloud_cover`: tile-level STAC metadata, not AOI cloud cover.
