"""Full Phase 1 workflow through the API and worker with synthetic STAC and rasters (no network).

create project → AOI → catalog search → submit snapshot → worker → evidence → provenance → layers →
tiles, plus the Phase 1 boundary (change → 422) and single-transaction failure semantics.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from aether.geo.stac.client import SearchResult, build_request, sort_items
from aether.jobs import queue
from aether.jobs.worker import run_job
from aether.storage.store import LocalFilesystemStore
from tests.factories import AOI_GEOJSON, SyntheticReader, stac_item

pytestmark = pytest.mark.db

PERIOD = {"start": "2024-06-01", "end": "2024-07-15"}


class FakeSearcher:
    api_url = "https://earth-search.test/v1"

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items
        self.calls = 0

    def search(self, geometry, start: date, end: date, max_tile_cloud: float) -> SearchResult:
        self.calls += 1
        req = build_request("sentinel-2-c1-l2a", geometry, start, end, max_tile_cloud)
        return SearchResult(self.api_url, "sentinel-2-c1-l2a", req, sort_items(self.items), len(self.items))


def _items():
    items = [stac_item(f"S2B_I{i}", datatake=f"DT{i}", dt=f"2024-06-{10 + i:02d}T18:58:21Z") for i in range(4)]
    items.append(stac_item("S2B_OLD", datatake="DTX", baseline="04.00", dt="2024-06-20T18:58:21Z"))
    return items


@pytest.fixture
def setup(clean, storage_root):
    from aether.api.app import app, searcher

    fake = FakeSearcher(_items())
    app.dependency_overrides[searcher] = lambda: fake
    client = TestClient(app)
    reader = SyntheticReader({"S2B_I0": 3100, "S2B_I1": 4100, "S2B_I2": 5100, "S2B_I3": 9000}, cloudy={"S2B_I3"})
    deps = {"searcher": fake, "reader": reader, "store": LocalFilesystemStore(storage_root)}
    yield client, fake, deps, clean
    app.dependency_overrides.clear()


def _project_aoi(client):
    p = client.post("/api/v1/projects", json={"name": "Davis test"}).json()
    r = client.post(f"/api/v1/projects/{p['id']}/aois", json={"name": "fields", "geometry": AOI_GEOJSON})
    assert r.status_code == 201, r.text
    return p, r.json()


def _tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    import math

    n = 2**z
    lat_r = math.radians(lat)
    return int((lon + 180) / 360 * n), int((1 - math.asinh(math.tan(lat_r)) / math.pi) / 2 * n)


def _run_worker(engine, deps):
    job = queue.claim(engine, "test-worker")
    assert job is not None
    run_job(engine, job, **deps)
    return job


def test_full_snapshot_workflow(setup):
    client, _fake, deps, engine = setup
    _project, aoi = _project_aoi(client)
    assert aoi["utm_epsg"] == 32610 and aoi["evidence_id"].startswith("ev_")
    aoi_ev = client.get(f"/api/v1/evidence/{aoi['evidence_id']}").json()
    assert aoi_ev["measurements"][0]["quantity"] == "aoi_area" and aoi_ev["measurements"][0]["unit"] == "m2"

    search = client.post("/api/v1/catalog/search", json={"aoi_id": aoi["id"], "period": PERIOD}).json()
    statuses = {s["item_id"]: (s["status"], s["reason_code"]) for s in search["scenes"]}
    assert statuses["S2B_OLD"] == ("excluded", "baseline_unsupported")
    assert search["observations_eligible"] == 4
    cloud = search["scenes"][0]["metadata_evidence"]["measurements"]
    assert {m["quantity"] for m in cloud} == {"tile_cloud_cover", "sun_elevation"}

    body = {"aoi_id": aoi["id"], "type": "snapshot", "period_a": PERIOD}
    sub = client.post("/api/v1/analyses", json=body)
    assert sub.status_code == 202, sub.text
    again = client.post("/api/v1/analyses", json=body)
    assert again.status_code == 200 and again.json()["analysis_run_id"] == sub.json()["analysis_run_id"]

    _run_worker(engine, deps)
    run_id = sub.json()["analysis_run_id"]
    detail = client.get(f"/api/v1/analyses/{run_id}").json()
    assert detail["status"] == "succeeded" and detail["outcome"] == "completed"
    assert detail["job"]["status"] == "succeeded"
    inputs = {i["item_id"]: (i["status"], i["reason_code"]) for i in detail["inputs"]}
    assert inputs["S2B_OLD"] == ("excluded", "baseline_unsupported") and inputs["S2B_I3"] == ("used", None)
    assert {c["calibration"] for c in detail["calibration"]} == {"untuned"}

    kinds = sorted(e["kind"] for e in detail["evidence"])
    assert kinds == ["coverage_statistic"] + ["index_statistic"] * 4
    cov_id = next(e["evidence_id"] for e in detail["evidence"] if e["kind"] == "coverage_statistic")
    cov = client.get(f"/api/v1/evidence/{cov_id}").json()
    m = {x["quantity"]: x for x in cov["measurements"]}
    assert m["coverage_a"]["denominator_quantity"] == "aoi_grid_area"
    assert m["coverage_a"]["denominator_measurement_id"] == m["aoi_grid_area"]["measurement_id"]
    assert m["coverage_a"]["value"] == pytest.approx(1.0)
    assert m["masked_fraction.cloud_shadow"]["value"] == pytest.approx(0.25)
    assert m["masked_fraction.cloud_shadow"]["denominator_quantity"] == "aoi_pixel_observations"
    assert m["observations_usable"]["value"] == 3
    assert {lim["code"] for lim in cov["limitations"]} >= {"scl_thin_cloud_omission", "scl_bright_surface_confusion"}

    for e in detail["evidence"]:
        ev = client.get(f"/api/v1/evidence/{e['evidence_id']}").json()
        for meas in ev["measurements"]:
            if meas["statistic"] == "fraction":
                assert meas["denominator_measurement_id"]  # I-8
            assert (meas["value"] is None) == (meas["status"] == "unavailable")
        # provenance completeness walk (C7 §9)
        prov = client.get(f"/api/v1/evidence/{e['evidence_id']}/provenance").json()
        used = [s for s in prov["scenes"] if s["status"] == "used"]
        assert used and all(s["assets"] and s["assets"][0]["href"].startswith("https://") for s in used)
        assert {"source_read", "radiometric_normalization", "masking", "index", "compositing", "statistics"} <= set(
            prov["step_kinds"]
        )
        assert prov["processing_run"]["code_version"]
        assert set(prov["processing_run"]["environment"]) >= {"python", "numpy", "rasterio", "gdal"}

    ndbi = next(e for e in detail["evidence"] if e["index"] == "ndbi")
    assert {lim["code"] for lim in ndbi["limitations"]} >= {"ndbi_not_builtup_detector", "index_resolution_20m"}

    layers = client.get(f"/api/v1/analyses/{run_id}/layers").json()
    names = {lyr["name"] for lyr in layers}
    assert {"true_colour", "index:ndvi", "index:ndbi", "n_valid:ndvi", "insufficient:ndvi"} <= names
    ndvi = next(lyr for lyr in layers if lyr["name"] == "index:ndvi")
    lon, lat = -121.771, 38.535
    tx, ty = _tile(lon, lat, 14)
    tile_url = ndvi["tile_url"].replace("{z}", "14").replace("{x}", str(tx)).replace("{y}", str(ty))
    resp = client.get(tile_url)
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
    assert client.get(f"/api/v1/tiles/{ndvi['artifact_id']}/14/{tx}/{ty}.png?render_name=rgb").status_code == 422


def test_change_requests_are_refused_in_phase1(setup):
    client, *_ = setup
    _, aoi = _project_aoi(client)
    r = client.post(
        "/api/v1/analyses",
        json={
            "aoi_id": aoi["id"],
            "type": "change",
            "period_a": PERIOD,
            "period_b": {"start": "2025-06-01", "end": "2025-07-15"},
        },
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "analysis_type_not_available"


def test_invalid_aoi_and_period_errors_are_structured(setup):
    client, *_ = setup
    p = client.post("/api/v1/projects", json={"name": "x"}).json()
    bad = client.post(
        f"/api/v1/projects/{p['id']}/aois",
        json={
            "name": "big",
            "geometry": {"type": "Polygon", "coordinates": [[[-121, 38], [-119, 38], [-119, 39], [-121, 39], [-121, 38]]]},
        },
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] in ("aoi_too_large", "aoi_crosses_utm_zone")
    _, aoi = _project_aoi(client)
    r = client.post("/api/v1/analyses", json={"aoi_id": aoi["id"], "period_a": {"start": "2024-06-01", "end": "2024-06-10"}})
    assert r.json()["error"]["code"] == "period_length_out_of_range"
    assert client.get("/api/v1/evidence/ev_" + "0" * 32).status_code == 404


def test_insufficient_observations_outcome(setup):
    client, fake, deps, engine = setup
    fake.items = _items()[:2]
    _, aoi = _project_aoi(client)
    sub = client.post("/api/v1/analyses", json={"aoi_id": aoi["id"], "period_a": PERIOD}).json()
    _run_worker(engine, deps)
    detail = client.get(f"/api/v1/analyses/{sub['analysis_run_id']}").json()
    assert detail["status"] == "succeeded" and detail["outcome"] == "insufficient_observations"
    ev_id = next(e["evidence_id"] for e in detail["evidence"] if e["index"] == "ndvi")
    ms = client.get(f"/api/v1/evidence/{ev_id}").json()["measurements"]
    stats = [m for m in ms if m["quantity"] == "index.ndvi"]
    assert stats and all(m["status"] == "unavailable" and m["value"] is None for m in stats)
    assert all(m["unavailable_reason"] == "insufficient_observations" for m in stats)


def test_failure_writes_no_partial_evidence(setup, monkeypatch):
    client, _fake, deps, engine = setup
    _, aoi = _project_aoi(client)
    sub = client.post("/api/v1/analyses", json={"aoi_id": aoi["id"], "period_a": PERIOD}).json()
    from aether.db import repo

    real = repo.insert_evidence
    calls = {"n": 0}

    def flaky(conn, ev, **kw):
        calls["n"] += 1
        if ev.kind == "index_statistic":
            raise RuntimeError("injected failure")
        return real(conn, ev, **kw)

    monkeypatch.setattr(repo, "insert_evidence", flaky)
    _run_worker(engine, deps)
    with engine.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM evidence WHERE analysis_run_id=:r"), {"r": sub["analysis_run_id"]}
        ).scalar_one()
        status = conn.execute(text("SELECT status FROM analysis_runs WHERE id=:r"), {"r": sub["analysis_run_id"]}).scalar_one()
    assert calls["n"] >= 2 and n == 0 and status == "failed"
    job = client.get(f"/api/v1/jobs/{sub['job_id']}").json()
    assert job["status"] == "failed" and job["error"]["code"] == "internal_error"


def test_retryable_error_requeues_then_fails(setup):
    client, _fake, deps, engine = setup
    from aether.domain.errors import AetherError

    _, aoi = _project_aoi(client)
    sub = client.post("/api/v1/analyses", json={"aoi_id": aoi["id"], "period_a": PERIOD}).json()

    class Down(FakeSearcher):
        def search(self, *a, **k):
            raise AetherError("stac_unavailable", "down", http_status=503, retryable=True)

    deps = {**deps, "searcher": Down([])}
    _run_worker(engine, deps)
    j = client.get(f"/api/v1/jobs/{sub['job_id']}").json()
    assert j["status"] == "queued" and j["error"]["code"] == "stac_unavailable" and j["attempts"] == 1
    with engine.begin() as conn:  # skip backoff for the test
        conn.execute(text("UPDATE jobs SET not_before = now(), attempts = max_attempts - 1 WHERE id=:j"), {"j": sub["job_id"]})
    _run_worker(engine, deps)
    assert client.get(f"/api/v1/jobs/{sub['job_id']}").json()["status"] == "failed"


def test_claim_is_exclusive_and_cancel_queued(setup):
    client, _fake, _deps, engine = setup
    _, aoi = _project_aoi(client)
    sub = client.post("/api/v1/analyses", json={"aoi_id": aoi["id"], "period_a": PERIOD}).json()
    first = queue.claim(engine, "w1")
    assert first is not None and queue.claim(engine, "w2") is None
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET heartbeat_at = now() - interval '10 minutes' WHERE id=:j"), {"j": first["id"]})
    assert queue.reap_stale(engine) == 1
    assert client.get(f"/api/v1/jobs/{sub['job_id']}").json()["status"] == "queued"
    c = client.post(f"/api/v1/jobs/{sub['job_id']}/cancel").json()
    assert c["status"] == "cancelled"
    assert client.get(f"/api/v1/analyses/{sub['analysis_run_id']}").json()["status"] == "cancelled"


def test_deduplicated_artifacts_still_belong_to_each_run(setup):
    """Identical inputs under two AOIs give byte-identical rasters (one artifact row each, content-
    addressed). The second run must still list every layer, including the display composite."""
    client, _fake, deps, engine = setup
    p = client.post("/api/v1/projects", json={"name": "dedup"}).json()
    runs = []
    for name in ("first", "second"):
        aoi = client.post(f"/api/v1/projects/{p['id']}/aois", json={"name": name, "geometry": AOI_GEOJSON}).json()
        runs.append(client.post("/api/v1/analyses", json={"aoi_id": aoi["id"], "period_a": PERIOD}).json())
        _run_worker(engine, deps)
    layers = [client.get(f"/api/v1/analyses/{r['analysis_run_id']}/layers").json() for r in runs]
    names = [{lyr["name"] for lyr in ls} for ls in layers]
    assert names[0] == names[1] and "true_colour" in names[1]
    assert {lyr["sha256"] for lyr in layers[0]} == {lyr["sha256"] for lyr in layers[1]}
    prov = client.get(f"/api/v1/analyses/{runs[1]['analysis_run_id']}").json()
    cov = next(e["evidence_id"] for e in prov["evidence"] if e["kind"] == "coverage_statistic")
    assert "display_composite" in client.get(f"/api/v1/evidence/{cov}/provenance").json()["step_kinds"]
