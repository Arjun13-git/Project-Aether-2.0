"""Reproducibility check (invariant I-12) for a persisted snapshot run.

Usage (from the repo root, API database configured in apps/api/.env):
  uv run --project apps/api python scripts/repro_check.py [ANALYSIS_RUN_ID ...]   # default: all completed runs

Re-executes each run from its *recorded* inputs, with no new catalogue search:
- the verbatim STAC JSON stored for its scenes;
- the same grid, parameters and algorithm versions.

Window reads come from the window cache (or the network if evicted). Fresh artifacts are written to
a throw-away store. The check compares their sha256 and every index statistic with what was
persisted. Exit code 1 on any difference.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from shapely.geometry import shape
from sqlalchemy import text

os.chdir(Path(__file__).resolve().parents[1] / "apps" / "api")  # so apps/api/.env is used

from aether.db import repo  # noqa: E402
from aether.db.engine import get_engine  # noqa: E402
from aether.geo.grid import build_analysis_grid  # noqa: E402
from aether.geo.io.windows import CachedAssetReader, RasterioAssetReader  # noqa: E402
from aether.geo.snapshot import run_snapshot  # noqa: E402
from aether.geo.stac.eligibility import apply_eligibility, parse_item  # noqa: E402
from aether.services.snapshot_run import _write_artifacts  # noqa: E402
from aether.settings import get_settings  # noqa: E402
from aether.storage.store import LocalFilesystemStore  # noqa: E402


def check(conn, run_id) -> bool:
    run = conn.execute(text("SELECT id, aoi_id, parameters, grid_hash FROM analysis_runs WHERE id=:r"), {"r": run_id}).one()
    aoi = repo.get_aoi(conn, run.aoi_id)
    items = [
        r.stac_json
        for r in conn.execute(
            text("SELECT s.stac_json FROM analysis_run_inputs i JOIN scenes s ON s.id=i.scene_id WHERE i.analysis_run_id=:r"),
            {"r": run_id},
        )
    ]
    stored_art = {tuple(a["band_names"]): a["sha256"] for a in repo.run_artifacts(conn, run_id)}
    stored_stats = {
        (r.quantity, r.statistic): r.value
        for r in conn.execute(
            text(
                "SELECT m.quantity, m.statistic, m.value FROM measurements m JOIN evidence e ON e.evidence_id=m.evidence_id "
                "WHERE e.analysis_run_id=:r AND m.quantity LIKE 'index.%'"
            ),
            {"r": run_id},
        )
    }
    area = conn.execute(
        text(
            "SELECT m.value FROM measurements m JOIN evidence e ON e.evidence_id=m.evidence_id "
            "WHERE e.analysis_run_id=:r AND m.quantity='aoi_area'"
        ),
        {"r": run_id},
    ).scalar_one()

    grid = build_analysis_grid(shape(aoi["geometry"]), aoi["utm_epsg"])
    assert grid.grid_hash == run.grid_hash, "grid hash differs"
    scenes = [parse_item(i) for i in items]
    apply_eligibility(scenes, grid.epsg)
    reader = CachedAssetReader(RasterioAssetReader(), Path(get_settings().storage_root).resolve())
    res = run_snapshot(grid=grid, aoi_area_m2=area, scenes=scenes, reader=reader, n_min=int(run.parameters["n_min"]))
    with tempfile.TemporaryDirectory() as tmp:
        fresh = _write_artifacts(res, LocalFilesystemStore(Path(tmp)))
    measured = {k: a for k, a in fresh.items() if a["kind"] != "composite"}  # index + n_valid
    meas_ok = sum(stored_art.get(tuple(a["raster"]["band_names"])) == a["sha256"] for a in measured.values())
    rgb = fresh.get("rgb")
    stored_rgb = stored_art.get(("red", "green", "blue"))
    display = (
        "n/a"
        if rgb is None
        else (
            "identical"
            if stored_rgb == rgb["sha256"]
            else "not linked to run (persisted before display_composite step)"
            if stored_rgb is None
            else "DIFFERENT"
        )
    )
    stat_diff = 0
    for name, z in res.stats.items():
        for stat in ("mean", "median", "p10", "p90"):
            if stored_stats.get((f"index.{name}", stat)) != getattr(z, stat):
                stat_diff += 1
    ok = meas_ok == len(measured) and stat_diff == 0 and display != "DIFFERENT"
    print(
        f"{run_id}  {aoi['name'][:34]:34}  measured artifacts identical {meas_ok}/{len(measured)}  "
        f"statistic mismatches {stat_diff}  display RGB {display}  {'PASS' if ok else 'FAIL'}"
    )
    return ok


def main() -> None:
    with get_engine().connect() as conn:
        ids = sys.argv[1:] or [
            r.id for r in conn.execute(text("SELECT id FROM analysis_runs WHERE outcome='completed' ORDER BY created_at"))
        ]
        results = [check(conn, i) for i in ids]
    print(f"reproducibility: {sum(results)}/{len(results)} runs bitwise-identical")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
