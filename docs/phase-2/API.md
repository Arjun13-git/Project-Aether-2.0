# Change-analysis API (`/api/v1`)

These are the change-specific endpoints and fields. Everything else (projects, AOIs, catalogue search, snapshots, jobs) is unchanged from [`../phase-1/API.md`](../phase-1/API.md). The full schema is at `http://127.0.0.1:8000/docs`. Errors use the Phase 1 body: `{"error": {"code", "message", "details", "request_id"}}`.

| Method & path | Change-analysis behaviour |
|---|---|
| `GET /health` | `capabilities` include `change_analysis`; `change_algorithm_versions` lists the change version map (snapshot `algorithm_versions` unchanged) |
| `POST /analyses/preflight` | Body `{aoi_id, type: "change", period_a, period_b}`. Returns pair-rule status, per-period `items_considered` / `observations_eligible`, the grid, `n_min`, `coregistration_assessable_by_extent` with a note, the C-03 outcome order and per-period scenes. Metadata only: no pixels are read |
| `POST /analyses` | Body `{aoi_id, type: "change", period_a: {start, end}, period_b: {start, end}}`. Any other field is `422 request_invalid` (no parameter or threshold overrides, C-09). `202` new run, `200` existing identical run (`cached: true`) |
| `GET /analyses/{id}` | Adds `period_b_start`/`period_b_end`, the change calibration table (all `untuned` or `product_choice`), evidence references with `period` (`a`/`b`/`delta`), `index` and `event_label`, and `inputs[]` with the recorded `period` (`a`/`b`; snapshot inputs record `a`) |
| `GET /analyses/{id}/layers` | Layers with `period` (`a`, `b`, `delta`, or `null` when identical bytes serve both periods), `render`, `tile_url` and `legend` |
| `GET /changes?analysis_run_id=…[&category=…][&bbox=minLon,minLat,maxLon,maxLat]` | GeoJSON FeatureCollection of persisted change events, ordered by `event_label`, with `feature_count` and `outcome` |
| `GET /evidence/{ev_id}` | Change evidence kinds: `coverage_statistic`, `index_statistic`, `coregistration_check`, `change_event`, `change_summary`. They carry `period_b_start`/`period_b_end`, the stored `confidence_band` and `geometry_artifact_id` (GeoPackage) for events |
| `GET /evidence/{ev_id}/provenance` | Steps carry `period`; scenes carry `period`; change steps: `qa_composite`, `coregistration`, `change_rules`, `morphology`, `polygonization` |
| `GET /tiles/{artifact_id}/{z}/{x}/{y}.png?render_name=` | Change renderings: `delta:ndvi`, `delta:mndwi`, `delta:ndbi`, `category`, `not_compared` |

## Change-specific errors

| Code | When |
|---|---|
| `422 periods_invalid` | A does not end before B starts, or a period is malformed |
| `422 period_length_out_of_range` | A period is outside 30–120 days |
| `422 period_before_archive` | A period starts before 2017-01-01 |
| `422 periods_not_season_matched` | Period midpoints are more than 30 days apart in the year |
| `422 request_invalid` | A change request carries fields other than `aoi_id`, `type`, `period_a`, `period_b` |
| `422 analysis_type_invalid` | `GET /changes` for a snapshot run |
| `422 category_invalid`, `422 bbox_invalid` | Bad `/changes` filter |
| `409 analysis_not_completed` | `GET /changes` for a run that is not `succeeded` (an empty collection would wrongly mean "no change") |
| `404` | Unknown run, evidence or artifact |

Scientific outcomes (`insufficient_observations`, `insufficient_coverage`, `coregistration_failed`) are **not** errors. The run has status `succeeded` and an `outcome`, plus the evidence explaining it.

## Event feature (`GET /changes`)

```json
{
  "type": "Feature",
  "id": 1,
  "geometry": {"type": "Polygon", "coordinates": ["…EPSG:4326 copy…"]},
  "properties": {
    "event_label": 1,
    "category": "built_bare_increase",
    "confidence_band": "higher",
    "mode": "robust",
    "touches_aoi_boundary": false,
    "index_flags": {"ndvi": ["-", "0"], "mndwi": ["0", "+"], "ndbi": ["0", "+"]},
    "evidence_id": "ev_…",
    "geometry_artifact_id": "…change_events.gpkg artifact…",
    "measurements": ["event_area", "event_pixel_count", "event_median_delta.<index>", "event_strength", "event_support_a/b", "event_flag_pixels.<index>.positive/negative"],
    "limitations": [{"code": "thresholds_untuned", "blocking": false, "text": "…"}]
  }
}
```

`measurements` holds full Measurement objects (value, unit, status, method); only the quantity names are shown above. The zero-sign pixel count is `event_pixel_count − positive − negative` and is not stored (C-18).
