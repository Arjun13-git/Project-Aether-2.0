"""Change-analysis API (implementation plan §12): preflight, submission, status, results, /changes, layers.

Synthetic worlds (tests/change_fixtures.py) behind the existing app; the worker runs in-process. Every
GET reads persisted rows only; nothing here re-runs the change engine.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from aether import science
from aether.domain.errors import AetherError
from aether.jobs import queue
from aether.jobs.worker import run_job
from aether.services import change_persistence
from aether.services.change_run import change_idempotency_key
from aether.storage.store import LocalFilesystemStore
from tests.change_fixtures import CX, PERIOD_A, PERIOD_B, ItemWorld, aoi_record, centred_box, world
from tests.integration.test_change_persistence import _overlap_world

pytestmark = pytest.mark.db

EVENT = (centred_box(200),)
PA = {"start": PERIOD_A.start.isoformat(), "end": PERIOD_A.end.isoformat()}
PB = {"start": PERIOD_B.start.isoformat(), "end": PERIOD_B.end.isoformat()}


class Ctx:
    def __init__(self, client: TestClient, engine: Any, storage_root: Any) -> None:
        self.client, self.engine, self.store = client, engine, LocalFilesystemStore(storage_root)
        self.searcher: Any = None
        self.reader: Any = None

    def use_world(self, **kw: Any) -> None:
        self.searcher, self.reader = world(**kw)

    def aoi(self, name: str, side: float = 2300) -> dict[str, Any]:
        p = self.client.post("/api/v1/projects", json={"name": f"change {name}"}).json()
        r = self.client.post(
            f"/api/v1/projects/{p['id']}/aois", json={"name": name, "geometry": aoi_record(side)["geometry"], "source": "geojson"}
        )
        assert r.status_code == 201, r.text
        return r.json()

    def body(self, aoi: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return {"aoi_id": aoi["id"], "type": "change", "period_a": PA, "period_b": PB, **extra}

    def run_worker(self) -> dict[str, Any]:
        job = queue.claim(self.engine, "test-worker")
        assert job is not None
        run_job(self.engine, job, searcher=self.searcher, reader=self.reader, store=self.store)
        return job

    def submit_and_run(self, name: str, side: float = 2300, **world_kw: Any) -> str:
        self.use_world(**world_kw)
        aoi = self.aoi(name, side)
        r = self.client.post("/api/v1/analyses", json=self.body(aoi))
        assert r.status_code == 202, r.text
        self.run_worker()
        return r.json()["analysis_run_id"]


@pytest.fixture
def ctx(clean, storage_root):
    from aether.api.app import app, searcher

    c = Ctx(TestClient(app), clean, storage_root)
    c.use_world()
    app.dependency_overrides[searcher] = lambda: c.searcher
    yield c
    app.dependency_overrides.clear()


# --- request validation ----------------------------------------------------------------------------


def test_invalid_change_requests_are_structured_4xx(ctx):
    aoi = ctx.aoi("validation")
    post = ctx.client.post
    cases = [
        ({k: v for k, v in ctx.body(aoi).items() if k != "period_b"}, "periods_invalid"),
        (ctx.body(aoi, period_a=PB, period_b=PA), "periods_invalid"),  # A must end before B starts
        (ctx.body(aoi, period_b={"start": "2024-01-01", "end": "2024-02-14"}), "periods_not_season_matched"),
        (ctx.body(aoi, period_b={"start": "2024-06-01", "end": "2024-06-10"}), "period_length_out_of_range"),
        (ctx.body(aoi, parameters={"k": 2.0}), "request_invalid"),  # C-09: no overrides
        (ctx.body(aoi, k=2.0, mmu_m2=1000), "request_invalid"),
        (ctx.body(aoi, collection="landsat-c2-l2"), "request_invalid"),
        (ctx.body(aoi, type="timeseries"), "request_invalid"),
    ]
    for body, code in cases:
        for path in ("/api/v1/analyses", "/api/v1/analyses/preflight"):
            r = post(path, json=body)
            assert r.status_code == 422, (path, body, r.text)
            assert r.json()["error"]["code"] == code, (path, body, r.json())
    r = post("/api/v1/analyses", json=ctx.body(aoi, parameters={"k": 2.0}))
    assert r.json()["error"]["details"]["unsupported_fields"] == ["parameters"]
    unknown = {**ctx.body(aoi), "aoi_id": "00000000-0000-0000-0000-00000000dead"}
    assert post("/api/v1/analyses", json=unknown).status_code == 404
    bad_geom = ctx.client.post(
        f"/api/v1/projects/{aoi['project_id']}/aois", json={"name": "bad", "geometry": {"type": "Point", "coordinates": [0, 0]}}
    )
    assert bad_geom.status_code == 422 and bad_geom.json()["error"]["code"]
    with ctx.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM analysis_runs")).scalar_one() == 0


def test_snapshot_request_semantics_are_unchanged(ctx):
    """Plan §18: snapshot requests keep Phase 1 semantics (extra fields ignored; period_b rejected)."""
    aoi = ctx.aoi("snapshot")
    ok = ctx.client.post("/api/v1/analyses", json={"aoi_id": aoi["id"], "type": "snapshot", "period_a": PA, "ignored": 1})
    assert ok.status_code == 202
    bad = ctx.client.post("/api/v1/analyses", json={"aoi_id": aoi["id"], "type": "snapshot", "period_a": PA, "period_b": PB})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "periods_invalid"


# --- preflight -------------------------------------------------------------------------------------


def test_preflight_discloses_counts_and_registration_assessability(ctx):
    aoi = ctx.aoi("preflight")
    r = ctx.client.post("/api/v1/analyses/preflight", json=ctx.body(aoi))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["type"] == "change" and out["pair_rules"] == "passed" and out["n_min"] == 3
    assert {p: (v["observations_eligible"], v["sufficient_observations_possible"]) for p, v in out["periods"].items()} == {
        "a": (6, True),
        "b": (6, True),
    }
    assert out["coregistration_assessable_by_extent"] is True and out["sufficient_observations_possible"] is True
    assert out["outcome_order"] == ["insufficient_observations", "insufficient_coverage", "coregistration_failed"]
    assert len(out["scenes"]["a"]) == 6 and len(out["scenes"]["b"]) == 6
    assert ctx.searcher.calls == [(PERIOD_A.start, PERIOD_A.end), (PERIOD_B.start, PERIOD_B.end)]
    with ctx.engine.connect() as conn:  # synchronous; nothing persisted
        assert conn.execute(text("SELECT count(*) FROM analysis_runs")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 0


def test_preflight_insufficient_observations_and_small_aoi_are_disclosures_not_gates(ctx):
    ctx.use_world(n_b=2)
    r = ctx.client.post("/api/v1/analyses/preflight", json=ctx.body(ctx.aoi("few")))
    assert r.status_code == 200
    out = r.json()
    assert out["periods"]["b"]["sufficient_observations_possible"] is False
    assert out["sufficient_observations_possible"] is False
    ctx.use_world()
    small = ctx.client.post("/api/v1/analyses/preflight", json=ctx.body(ctx.aoi("small", side=1500))).json()
    assert small["coregistration_assessable_by_extent"] is False
    assert "coregistration_not_verified" in small["coregistration_note"]
    # Coverage cannot be known from STAC metadata: preflight still succeeds on a cloudy B period.
    ctx.use_world(b=ItemWorld(cloud_west_of=CX + 300))
    cloudy = ctx.client.post("/api/v1/analyses/preflight", json=ctx.body(ctx.aoi("cloudy")))
    assert cloudy.status_code == 200 and "joint coverage" in cloudy.json()["note"]


# --- submission, idempotency, status ----------------------------------------------------------------


def test_submission_is_idempotent_and_creates_only_a_change_run(ctx):
    aoi = ctx.aoi("submit")
    first = ctx.client.post("/api/v1/analyses", json=ctx.body(aoi))
    assert first.status_code == 202 and first.json()["cached"] is False
    # Key order in the request cannot change identity.
    reordered = {"period_b": PB, "type": "change", "period_a": PA, "aoi_id": aoi["id"]}
    again = ctx.client.post("/api/v1/analyses", json=reordered)
    assert again.status_code == 200 and again.json()["analysis_run_id"] == first.json()["analysis_run_id"]
    with ctx.engine.connect() as conn:
        rows = conn.execute(text("SELECT type, idempotency_key, status FROM analysis_runs")).mappings().all()
        jobs = conn.execute(text("SELECT kind, status FROM jobs")).mappings().all()
        aoi_row = conn.execute(text("SELECT id, geometry_hash FROM aois WHERE id=:i"), {"i": aoi["id"]}).mappings().one()
    assert [(r["type"], r["status"]) for r in rows] == [("change", "queued")]  # no snapshot run created
    assert rows[0]["idempotency_key"] == change_idempotency_key(dict(aoi_row), PERIOD_A, PERIOD_B)
    assert [(j["kind"], j["status"]) for j in jobs] == [("analysis_run", "queued")]
    detail = ctx.client.get(f"/api/v1/analyses/{first.json()['analysis_run_id']}").json()
    assert detail["status"] == "queued" and detail["outcome"] is None and detail["job"]["status"] == "queued"
    assert detail["algorithm_versions"] == science.CHANGE_ALGORITHM_VERSIONS
    assert detail["parameters"] == science.change_parameters()


def test_status_distinguishes_running_failed_and_scientific_outcomes(ctx, monkeypatch):
    ctx.use_world(n_b=2)
    aoi = ctx.aoi("status")
    run_id = ctx.client.post("/api/v1/analyses", json=ctx.body(aoi)).json()["analysis_run_id"]
    job = queue.claim(ctx.engine, "w")
    assert ctx.client.get(f"/api/v1/analyses/{run_id}").json()["status"] == "running"
    run_job(ctx.engine, job, searcher=ctx.searcher, reader=ctx.reader, store=ctx.store)
    done = ctx.client.get(f"/api/v1/analyses/{run_id}").json()
    # A scientific outcome is a successful run, not an infrastructure failure.
    assert (done["status"], done["outcome"], done["job"]["status"]) == ("succeeded", "insufficient_observations", "succeeded")

    ctx.use_world()
    aoi2 = ctx.aoi("failing")
    run2 = ctx.client.post("/api/v1/analyses", json=ctx.body(aoi2)).json()["analysis_run_id"]

    def boom(*a: Any, **k: Any) -> str:
        raise AetherError("asset_read_failed", "simulated infrastructure failure", retryable=False)

    monkeypatch.setattr(change_persistence, "execute", boom)
    ctx.run_worker()
    failed = ctx.client.get(f"/api/v1/analyses/{run2}").json()
    assert (failed["status"], failed["outcome"]) == ("failed", None)
    assert failed["job"]["status"] == "failed" and failed["job"]["error"]["code"] == "asset_read_failed"
    r = ctx.client.get("/api/v1/changes", params={"analysis_run_id": run2})
    assert r.status_code == 409 and r.json()["error"]["code"] == "analysis_not_completed"


# --- results ---------------------------------------------------------------------------------------


def test_completed_result_detail_evidence_and_provenance(ctx):
    run_id = ctx.submit_and_run("result", b=ItemWorld(event_boxes=EVENT))
    detail = ctx.client.get(f"/api/v1/analyses/{run_id}").json()
    assert (detail["status"], detail["outcome"]) == ("succeeded", "completed")
    assert (detail["period_start"], detail["period_b_start"], detail["period_b_end"]) == (PA["start"], PB["start"], PB["end"])
    assert {c["name"] for c in detail["calibration"]} >= {
        "k",
        "a_NDVI",
        "mmu_m2",
        "band_higher_min_strength",
        "coreg_max_residual_px",
    }
    assert {c["calibration"] for c in detail["calibration"]} == {"untuned", "product_choice"}
    with ctx.engine.connect() as conn:
        recorded = dict(
            conn.execute(
                text(
                    "SELECT s.item_id, i.period FROM analysis_run_inputs i JOIN scenes s ON s.id = i.scene_id "
                    "WHERE i.analysis_run_id = :r"
                ),
                {"r": run_id},
            ).all()
        )
    assert {i["item_id"]: i["period"] for i in detail["inputs"]} == recorded  # exposed exactly as recorded
    assert {i["period"] for i in detail["inputs"]} == {"a", "b"}
    kinds = [e["kind"] for e in detail["evidence"]]
    assert kinds.count("change_event") == 1 and kinds.count("change_summary") == 1 and kinds.count("coregistration_check") == 1
    idx = [(e["index"], e["period"]) for e in detail["evidence"] if e["kind"] == "index_statistic"]
    assert sorted(idx) == sorted(
        [(n, p) for p in ("a", "b") for n in science.INDICES] + [(n, "delta") for n in science.CHANGE_INDICES]
    )

    ev_ref = next(e for e in detail["evidence"] if e["kind"] == "change_event")
    assert ev_ref["event_label"] == 1
    ev = ctx.client.get(f"/api/v1/evidence/{ev_ref['evidence_id']}").json()
    assert ev["confidence_band"] in ("higher", "moderate", "lower") and ev["confidence_band"] != "n/a"
    assert (ev["period_start"], ev["period_b_start"]) == (PA["start"], PB["start"])
    assert ev["geometry_artifact_id"] is not None
    ms = {(m["quantity"], m["statistic"]): m for m in ev["measurements"]}
    assert ms[("event_flag_pixels.ndvi.negative", "count")]["value"] == ms[("event_pixel_count", "count")]["value"]
    assert all(m["region"] == "event" and m["geometry_evidence_id"] == ev["evidence_id"] for m in ev["measurements"])

    coreg_ref = next(e for e in detail["evidence"] if e["kind"] == "coregistration_check")
    coreg = ctx.client.get(f"/api/v1/evidence/{coreg_ref['evidence_id']}").json()
    cms = {m["quantity"]: m for m in coreg["measurements"]}
    assert (
        coreg["facts"]["status"] == "ok"
        and cms["coreg_residual"]["unit"] == "px"
        and cms["coreg_residual"]["status"] == "available"
    )

    delta_ref = next(e for e in detail["evidence"] if e["period"] == "delta" and e["index"] == "ndvi")
    delta = ctx.client.get(f"/api/v1/evidence/{delta_ref['evidence_id']}").json()
    dms = {(m["quantity"], m["statistic"]): m for m in delta["measurements"]}
    assert dms[("index_delta_support.ndvi", "fraction")]["denominator_quantity"] == "aoi_grid_pixels_10m"
    assert not any(q.endswith(".ndwi") for q, _ in dms)

    prov = ctx.client.get(f"/api/v1/evidence/{ev_ref['evidence_id']}/provenance").json()
    assert {s["period"] for s in prov["scenes"]} == {"a", "b"} and all(s["status"] == "used" for s in prov["scenes"])
    assert {"qa_composite", "coregistration", "change_rules", "morphology", "polygonization"} <= set(prov["step_kinds"])
    assert prov["processing_run"]["algorithm_versions"] == science.CHANGE_ALGORITHM_VERSIONS


def test_scientific_outcomes_are_results_not_errors(ctx):
    for name, kw, side, outcome, lim in [
        ("cov", {"b": ItemWorld(cloud_west_of=CX + 300, event_boxes=EVENT)}, 2300, "insufficient_coverage", None),
        ("reg", {"b": ItemWorld(shift_px=2, event_boxes=EVENT)}, 2300, "coregistration_failed", None),
        ("nv", {"b": ItemWorld(event_boxes=EVENT)}, 1500, "completed", "coregistration_not_verified"),
    ]:
        run_id = ctx.submit_and_run(name, side=side, **kw)
        detail = ctx.client.get(f"/api/v1/analyses/{run_id}").json()
        assert (detail["status"], detail["outcome"]) == ("succeeded", outcome)
        fc = ctx.client.get("/api/v1/changes", params={"analysis_run_id": run_id})
        assert fc.status_code == 200 and fc.json()["outcome"] == outcome
        if lim is None:
            assert fc.json()["features"] == []
            coreg = next(e for e in detail["evidence"] if e["kind"] == "coregistration_check")
            facts = ctx.client.get(f"/api/v1/evidence/{coreg['evidence_id']}").json()["facts"]
            assert facts["status"] == ("not_run" if outcome == "insufficient_coverage" else "failed")
        else:
            feats = fc.json()["features"]
            assert feats and all(f["properties"]["confidence_band"] == "lower" for f in feats)
            assert all(any(x["code"] == lim and x["blocking"] for x in f["properties"]["limitations"]) for f in feats)


# --- /changes: C-17 overlap, C-18 flags, GeoPackage reference --------------------------------------


def test_changes_feature_collection_preserves_overlap_flags_and_geometry_reference(ctx):
    run_id = ctx.submit_and_run("overlap", b=_overlap_world())
    r = ctx.client.get("/api/v1/changes", params={"analysis_run_id": run_id})
    assert r.status_code == 200, r.text
    fc = r.json()
    feats = fc["features"]
    assert fc["type"] == "FeatureCollection" and fc["outcome"] == "completed" and fc["feature_count"] == len(feats)
    assert [f["id"] for f in feats] == list(range(1, len(feats) + 1)) == [f["properties"]["event_label"] for f in feats]
    cats = [f["properties"]["category"] for f in feats]
    assert {"veg_loss", "veg_gain"} <= set(cats)
    (gpkg_id,) = {f["properties"]["geometry_artifact_id"] for f in feats}
    with ctx.engine.connect() as conn:
        assert conn.execute(text("SELECT kind FROM artifacts WHERE id=:i"), {"i": gpkg_id}).scalar_one() == "vector_gpkg"
    import shapely
    from shapely.geometry import shape

    geoms = [shape(f["geometry"]) for f in feats]
    # The EPSG:4326 copy reprojects vertices (C5 §7), so edges that merely touch can produce sub-mm²
    # slivers; the shared 10 m pixel (~1e-8 deg²) is the only real overlap (the UTM GeoPackage is exact).
    pixel_deg2 = 1e-9
    overlapping = [
        (i, j) for i in range(len(geoms)) for j in range(i + 1, len(geoms)) if geoms[i].intersection(geoms[j]).area > pixel_deg2
    ]
    assert len(overlapping) == 1 and cats[overlapping[0][0]] != cats[overlapping[0][1]]  # separate, not merged
    assert all(shapely.is_valid(g) for g in geoms)
    for f in feats:
        p = f["properties"]
        ms = {(m["quantity"], m["statistic"]): m["value"] for m in p["measurements"]}
        n = ms[("event_pixel_count", "count")]
        for i in science.CHANGE_INDICES:
            pos, neg = ms[(f"event_flag_pixels.{i}.positive", "count")], ms[(f"event_flag_pixels.{i}.negative", "count")]
            zero = n - pos - neg
            expected = [s for s, c in (("-", neg), ("0", zero), ("+", pos)) if c > 0]
            assert p["index_flags"][i] == expected and expected  # C-18: signs present, fixed order, never empty
        assert p["index_flags"]["mndwi"] == ["0"]  # all-zero index
    assert any(f["properties"]["index_flags"]["ndvi"] == ["-", "+"] for f in feats)  # mixed signs (overlap event)

    only_gain = ctx.client.get("/api/v1/changes", params={"analysis_run_id": run_id, "category": "veg_gain"}).json()
    assert [f["properties"]["category"] for f in only_gain["features"]] == ["veg_gain"]
    minx, miny, maxx, maxy = shape(feats[0]["geometry"]).bounds
    boxed = ctx.client.get("/api/v1/changes", params={"analysis_run_id": run_id, "bbox": f"{minx},{miny},{maxx},{maxy}"}).json()
    assert 1 in [f["id"] for f in boxed["features"]]
    far = ctx.client.get("/api/v1/changes", params={"analysis_run_id": run_id, "bbox": "10,10,11,11"}).json()
    assert far["features"] == []

    # Deterministic responses for identical persisted inputs.
    assert ctx.client.get("/api/v1/changes", params={"analysis_run_id": run_id}).json() == fc


def test_changes_errors_and_zero_event_runs(ctx):
    run_id = ctx.submit_and_run("zero")
    fc = ctx.client.get("/api/v1/changes", params={"analysis_run_id": run_id}).json()
    assert (fc["outcome"], fc["feature_count"], fc["features"]) == ("completed", 0, [])
    get = ctx.client.get
    for params, status, code in [
        ({"analysis_run_id": run_id, "bbox": "1,2,3"}, 422, "bbox_invalid"),
        ({"analysis_run_id": run_id, "bbox": "5,5,1,1"}, 422, "bbox_invalid"),
        ({"analysis_run_id": run_id, "category": "forest"}, 422, "category_invalid"),
        ({"analysis_run_id": "00000000-0000-0000-0000-00000000beef"}, 404, "not_found"),
    ]:
        r = get("/api/v1/changes", params=params)
        assert (r.status_code, r.json()["error"]["code"]) == (status, code)
    aoi = ctx.aoi("pending")
    pending = ctx.client.post("/api/v1/analyses", json=ctx.body(aoi)).json()["analysis_run_id"]
    r = get("/api/v1/changes", params={"analysis_run_id": pending})
    assert r.status_code == 409 and r.json()["error"]["details"]["status"] == "queued"
    snap = ctx.client.post("/api/v1/analyses", json={"aoi_id": aoi["id"], "type": "snapshot", "period_a": PA}).json()
    r = get("/api/v1/changes", params={"analysis_run_id": snap["analysis_run_id"]})
    assert (r.status_code, r.json()["error"]["code"]) == (422, "analysis_type_invalid")
    assert get("/api/v1/analyses/00000000-0000-0000-0000-00000000beef").status_code == 404


# --- layers, tiles, health ---------------------------------------------------------------------------


def _tile_xy(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2**z
    return int((lon + 180) / 360 * n), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)


def test_change_layers_periods_and_renderings(ctx):
    run_id = ctx.submit_and_run("layers", b=ItemWorld(event_boxes=EVENT))
    layers = ctx.client.get(f"/api/v1/analyses/{run_id}/layers").json()
    by = {(lyr["name"], lyr["period"]) for lyr in layers}
    assert {("true_colour", "a"), ("true_colour", "b"), ("index:ndvi", "a"), ("index:ndvi", "b")} <= by
    assert {
        ("delta:ndvi", "delta"),
        ("delta:mndwi", "delta"),
        ("delta:ndbi", "delta"),
        ("category", "delta"),
        ("not_compared", "delta"),
    } <= by
    assert not any(lyr["kind"] in ("z", "event_label", "vector_gpkg") for lyr in layers)  # no z layers (C-08)
    assert sum(lyr["name"] == "true_colour" for lyr in layers) == 2  # NIR QA composites are not map layers
    delta = next(lyr for lyr in layers if lyr["name"] == "delta:ndvi")
    assert delta["legend"]["range"] == [-0.5, 0.5] and delta["legend"]["note"] == "Δ = B − A · positive = index increased"
    from shapely.geometry import shape

    lon, lat = shape(aoi_record(2300)["geometry"]).centroid.coords[0]
    tx, ty = _tile_xy(lon, lat, 14)
    for name in ("delta:ndvi", "delta:mndwi", "category", "not_compared"):
        lyr = next(x for x in layers if x["name"] == name)
        url = lyr["tile_url"].replace("{z}", "14").replace("{x}", str(tx)).replace("{y}", str(ty))
        resp = ctx.client.get(url)
        assert resp.status_code == 200 and resp.headers["content-type"] == "image/png", (name, resp.text)
        assert ctx.client.get(f"/api/v1/tiles/{lyr['artifact_id']}/14/{tx}/{ty}.png?render_name=rgb").status_code == 422
    health = ctx.client.get("/api/v1/health").json()
    assert "change_analysis" in health["capabilities"] and health["not_available"] == ["analyst", "reports"]
    assert health["algorithm_versions"] == science.SNAPSHOT_ALGORITHM_VERSIONS
    assert health["change_algorithm_versions"] == science.CHANGE_ALGORITHM_VERSIONS
