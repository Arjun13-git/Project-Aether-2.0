"""Phase 1 end-to-end validation on civilian demo AOIs, driven only through the public API.

Usage (API + worker running):  uv run --project apps/api python scripts/demo_phase1.py [--out FILE]

Prints and saves what the system measured. Every number printed is read from a Measurement
returned by the API; this script computes nothing itself.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import httpx

API = "http://127.0.0.1:8000/api/v1"

# Civilian demo AOIs (C1 §2: summer 2024 has C1 coverage). Small boxes, one UTM zone each.
DEMO_AOIS: list[dict[str, Any]] = [
    {"name": "Davis CA — agriculture/vegetation", "bbox": (-121.80, 38.52, -121.77, 38.545)},
    {"name": "Las Vegas NV — urban/infrastructure", "bbox": (-115.180, 36.100, -115.155, 36.122)},
    {"name": "Alviso CA — water/coastal salt ponds", "bbox": (-122.000, 37.440, -121.970, 37.465)},
]
PERIOD = {"start": "2024-06-01", "end": "2024-08-15"}


def bbox_polygon(b: tuple[float, float, float, float]) -> dict[str, Any]:
    x0, y0, x1, y1 = b
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


def ok(r: httpx.Response) -> Any:
    if r.status_code >= 400:
        raise SystemExit(f"{r.request.method} {r.request.url} → {r.status_code}: {r.text}")
    return r.json()


def measurements(c: httpx.Client, ev_id: str) -> dict[str, dict[str, Any]]:
    ev = ok(c.get(f"{API}/evidence/{ev_id}"))
    return {f"{m['quantity']}|{m['statistic']}": m for m in ev["measurements"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    results = []
    with httpx.Client(timeout=300) as c:
        project = ok(c.post(f"{API}/projects", json={"name": f"Phase 1 validation {time.strftime('%Y-%m-%d %H:%M')}"}))
        for demo in DEMO_AOIS:
            t0 = time.perf_counter()
            aoi = ok(
                c.post(
                    f"{API}/projects/{project['id']}/aois",
                    json={"name": demo["name"], "geometry": bbox_polygon(demo["bbox"]), "source": "geojson"},
                )
            )
            search = ok(c.post(f"{API}/catalog/search", json={"aoi_id": aoi["id"], "period": PERIOD}))
            sub = ok(c.post(f"{API}/analyses", json={"aoi_id": aoi["id"], "type": "snapshot", "period_a": PERIOD}))
            while True:
                job = ok(c.get(f"{API}/jobs/{sub['job_id']}"))
                if job["status"] in ("succeeded", "failed", "cancelled"):
                    break
                print(f"  {demo['name'][:24]}: {job['stage']} {job['progress']:.2f}", file=sys.stderr)
                time.sleep(5)
            detail = ok(c.get(f"{API}/analyses/{sub['analysis_run_id']}"))
            evs = {e["index"] or e["kind"]: e["evidence_id"] for e in detail["evidence"]}
            cov = measurements(c, evs["coverage_statistic"]) if "coverage_statistic" in evs else {}
            idx = {name: measurements(c, evs[name]) for name in ("ndvi", "ndwi", "mndwi", "ndbi") if name in evs}
            prov = ok(c.get(f"{API}/evidence/{evs['coverage_statistic']}/provenance")) if cov else {}
            layers = ok(c.get(f"{API}/analyses/{sub['analysis_run_id']}/layers"))
            reasons: dict[str, int] = {}
            for s in search["scenes"]:
                if s["status"] == "excluded":
                    reasons[s["reason_code"]] = reasons.get(s["reason_code"], 0) + 1
            results.append(
                {
                    "aoi": demo["name"],
                    "aoi_id": aoi["id"],
                    "utm_epsg": aoi["utm_epsg"],
                    "analysis_run_id": sub["analysis_run_id"],
                    "job_status": job["status"],
                    "job_error": job["error"],
                    "outcome": detail["outcome"],
                    "wall_time_s": round(time.perf_counter() - t0, 1),
                    "search": {
                        "items": len(search["scenes"]),
                        "eligible_observations": search["observations_eligible"],
                        "excluded_by_reason": reasons,
                    },
                    "run_inputs": {
                        "used": sum(i["status"] == "used" for i in detail["inputs"]),
                        "excluded": [i for i in detail["inputs"] if i["status"] == "excluded"],
                    },
                    "coverage": {
                        k: {f: v[f] for f in ("value", "unit", "status", "unavailable_reason", "denominator_quantity")}
                        for k, v in cov.items()
                    },
                    "indices": {
                        n: {k: (v["value"], v["status"], v["unavailable_reason"]) for k, v in m.items()} for n, m in idx.items()
                    },
                    "timings_s": prov.get("processing_run", {}).get("timings"),
                    "code_version": prov.get("processing_run", {}).get("code_version"),
                    "grid": {lyr["name"]: lyr["resolution_m"] for lyr in layers},
                }
            )
            print(json.dumps(results[-1], indent=1, default=str))
    if args.out:
        with open(args.out, "w") as f:
            json.dump(results, f, indent=1, default=str)


if __name__ == "__main__":
    main()
