"""FastAPI application: Phase 1 API surface under /api/v1 (BACKEND-DESIGN.md)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import Connection

from aether import science
from aether.api import schemas as S
from aether.api import tiles
from aether.db import repo
from aether.db.engine import allow_project_delete, get_engine
from aether.domain.errors import AetherError, NotFound
from aether.domain.registries import LIMITATIONS
from aether.geo.stac.client import EarthSearchClient, StacSearcher
from aether.jobs import queue
from aether.services import analyses, aois, catalog, provenance_view
from aether.settings import get_settings

log = logging.getLogger("aether.api")


def db() -> Iterator[Connection]:
    with get_engine().begin() as conn:
        yield conn


def searcher() -> StacSearcher:
    s = get_settings()
    return EarthSearchClient(s.stac_api_url, s.stac_collection)


def _error(request: Request, status: int, code: str, message: str, details: dict[str, Any] | None = None) -> JSONResponse:
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex
    body = {"error": {"code": code, "message": message, "details": details or {}, "request_id": rid}}
    return JSONResponse(status_code=status, content=body)


r = APIRouter(prefix="/api/v1")
ERR: dict[int | str, dict[str, Any]] = {422: {"model": S.ErrorResponse}, 404: {"model": S.ErrorResponse}}


@r.get("/health", tags=["meta"])
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "algorithm_versions": science.ALGORITHM_VERSIONS,
        "phase": 1,
        "capabilities": ["aoi", "catalog_search", "snapshot_analysis", "evidence", "tiles"],
        "not_available": ["change_analysis", "analyst", "reports"],
    }


@r.get("/projects", response_model=list[S.Project], tags=["projects"])
def list_projects(conn: Connection = Depends(db)) -> Any:
    return repo.list_projects(conn)


@r.post("/projects", response_model=S.Project, status_code=201, tags=["projects"])
def create_project(body: S.ProjectIn, conn: Connection = Depends(db)) -> Any:
    return repo.insert_project(conn, body.name, body.description)


@r.get("/projects/{project_id}", response_model=S.Project, responses=ERR, tags=["projects"])
def get_project(project_id: uuid.UUID, conn: Connection = Depends(db)) -> Any:
    return repo.get_project(conn, project_id)


@r.delete("/projects/{project_id}", status_code=204, responses=ERR, tags=["projects"])
def delete_project(project_id: uuid.UUID, conn: Connection = Depends(db)) -> None:
    from sqlalchemy import text

    repo.get_project(conn, project_id)
    allow_project_delete(conn)
    conn.execute(text("DELETE FROM projects WHERE id=:id"), {"id": project_id})


@r.post("/projects/{project_id}/aois", response_model=S.Aoi, status_code=201, responses=ERR, tags=["aois"])
def create_aoi(project_id: uuid.UUID, body: S.AoiIn, conn: Connection = Depends(db)) -> Any:
    return aois.create_aoi(conn, project_id=project_id, name=body.name, geometry=body.geometry, source=body.source)


@r.get("/projects/{project_id}/aois", response_model=list[S.Aoi], tags=["aois"])
def list_aois(project_id: uuid.UUID, conn: Connection = Depends(db)) -> Any:
    return repo.list_aois(conn, project_id)


@r.get("/aois/{aoi_id}", response_model=S.Aoi, responses=ERR, tags=["aois"])
def get_aoi(aoi_id: uuid.UUID, conn: Connection = Depends(db)) -> Any:
    return repo.get_aoi(conn, aoi_id)


@r.post("/catalog/search", response_model=S.CatalogSearchOut, responses=ERR, tags=["catalog"])
def catalog_search(body: S.CatalogSearchIn, conn: Connection = Depends(db), st: StacSearcher = Depends(searcher)) -> Any:
    from aether.domain.periods import Period, validate_period

    period = Period(body.period.start, body.period.end)
    validate_period(period)
    return catalog.search_and_record(conn, st, aoi_id=body.aoi_id, period=period, max_tile_cloud=body.max_tile_cloud)


@r.post("/analyses/preflight", responses=ERR, tags=["analyses"])
def preflight(body: S.AnalysisIn, conn: Connection = Depends(db), st: StacSearcher = Depends(searcher)) -> Any:
    return analyses.preflight(
        conn,
        st,
        aoi_id=body.aoi_id,
        analysis_type=body.type,
        period_a=body.period_a.model_dump(mode="json"),
        period_b=body.period_b.model_dump(mode="json") if body.period_b else None,
    )


@r.post("/analyses", response_model=S.AnalysisSubmitted, status_code=202, responses=ERR, tags=["analyses"])
def submit_analysis(body: S.AnalysisIn, conn: Connection = Depends(db)) -> Any:
    out, created = analyses.submit(
        conn,
        aoi_id=body.aoi_id,
        analysis_type=body.type,
        period_a=body.period_a.model_dump(mode="json"),
        period_b=body.period_b.model_dump(mode="json") if body.period_b else None,
    )
    if not created:
        return JSONResponse(status_code=200, content=S.AnalysisSubmitted(**out).model_dump(mode="json"))
    return out


@r.get("/projects/{project_id}/analyses", response_model=list[S.AnalysisSummary], tags=["analyses"])
def list_analyses(project_id: uuid.UUID, conn: Connection = Depends(db)) -> Any:
    return analyses.list_analyses(conn, project_id)


@r.get("/analyses/{analysis_id}", response_model=S.AnalysisDetail, responses=ERR, tags=["analyses"])
def get_analysis(analysis_id: uuid.UUID, conn: Connection = Depends(db)) -> Any:
    return analyses.get_analysis(conn, analysis_id)


@r.get("/analyses/{analysis_id}/layers", response_model=list[S.Layer], responses=ERR, tags=["analyses"])
def layers(analysis_id: uuid.UUID, conn: Connection = Depends(db)) -> Any:

    analyses.get_analysis(conn, analysis_id)
    out: list[dict[str, Any]] = []
    for row in repo.run_artifacts(conn, analysis_id):
        for rend in tiles.renderings_for(row["kind"], row["band_names"]):
            if row["kind"] == "composite":
                label = "true_colour"
            elif row["kind"] == "n_valid":
                label = f"{rend.name}:{row['band_names'][0].removeprefix('n_valid_')}"
            else:
                label = rend.name
            layer = {
                "artifact_id": row["id"],
                "name": label,
                "kind": row["kind"],
                "render": rend.name,
                "tile_url": f"/api/v1/tiles/{row['id']}/{{z}}/{{x}}/{{y}}.png?render_name={rend.name}",
                "legend": rend.legend,
                "grid": "G10" if row["resolution_m"] == 10 else "G20",
                "resolution_m": row["resolution_m"],
                "sha256": row["sha256"],
            }
            if row["kind"] == "composite":
                out.insert(0, layer)  # the display composite (true colour) is listed first
            else:
                out.append(layer)
    return out


@r.get("/analyses/{run_a}/compare/{run_b}", response_model=S.CompareOut, responses=ERR, tags=["analyses"])
def compare(run_a: uuid.UUID, run_b: uuid.UUID, conn: Connection = Depends(db)) -> Any:
    """Whether two snapshots may be shown side by side (C4 §1). Computes no measurement."""
    return analyses.compare_check(conn, run_a, run_b)


@r.get("/jobs/{job_id}", response_model=S.Job, responses=ERR, tags=["jobs"])
def get_job(job_id: uuid.UUID, conn: Connection = Depends(db)) -> Any:
    job = queue.get_job(conn, job_id)
    if job is None:
        raise NotFound("job", str(job_id))
    return job


@r.post("/jobs/{job_id}/cancel", response_model=S.Job, responses=ERR, tags=["jobs"])
def cancel_job(job_id: uuid.UUID, conn: Connection = Depends(db)) -> Any:
    if queue.request_cancel(conn, job_id) is None and queue.get_job(conn, job_id) is None:
        raise NotFound("job", str(job_id))
    return queue.get_job(conn, job_id)


@r.get("/evidence/{evidence_id}", response_model=S.Evidence, responses=ERR, tags=["evidence"])
def get_evidence(evidence_id: str, conn: Connection = Depends(db)) -> Any:
    ev = repo.get_evidence(conn, evidence_id)
    ev["limitations"] = [{**lim, "text": LIMITATIONS[lim["code"]].text} for lim in ev["limitations"]]
    return ev


@r.get("/evidence/{evidence_id}/provenance", responses=ERR, tags=["evidence"])
def get_provenance(evidence_id: str, conn: Connection = Depends(db)) -> Any:
    return provenance_view.expand(conn, evidence_id)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Aether 2.0 API",
        version="0.1.0-phase1",
        description="Evidence-first Earth-observation snapshot analysis (Phase 1). "
        "Change analysis, the analyst and reports are not available in Phase 1.",
    )

    @app.exception_handler(AetherError)
    async def aether_error(request: Request, exc: AetherError) -> JSONResponse:
        if exc.http_status >= 500:
            log.error("error %s: %s", exc.code, exc.message)
        return _error(request, exc.http_status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(
            request,
            422,
            "request_invalid",
            "request validation failed",
            {"errors": [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]},
        )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error")
        return _error(request, 500, "internal_error", "internal error")

    app.include_router(r)
    tiles_router = APIRouter(prefix="/api/v1")
    tiles_router.include_router(tiles.router)
    app.include_router(tiles_router)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    s = get_settings()
    logging.basicConfig(level=s.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run("aether.api.app:app", host=s.bind_host, port=8000, log_level=s.log_level.lower())


if __name__ == "__main__":
    main()
