import logging
from typing import Awaitable, TypeVar
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from neo4j.exceptions import AuthError, Neo4jError, ServiceUnavailable, SessionExpired
from schemas.dto import (
    Track1AnalysisRequest, Track1AnalysisResponse, Track1LimitAnalysisRequest, Track1LimitAnalysisResponse,
    Track1BulkClusterMemberPage, Track1BulkJobCreateRequest, Track1BulkJobAccepted, Track1BulkJobResults, Track1BulkJobStatus,
)
from core.dependencies import get_graph_service, get_track1_bulk_job_service
from services.graph_service import GraphAnalysisUnavailable, GraphResultInvalid, GraphService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/clusters", tags=["Track 1 (Macro)"])
v2_router = APIRouter(prefix="/clusters", tags=["Track 1 (Purchase limit)"])
bulk_router = APIRouter(prefix="/clusters/jobs", tags=["Track 1 (Bulk analysis jobs)"])

AnalysisResult = TypeVar("AnalysisResult")


async def _run_analysis(analysis: Awaitable[AnalysisResult]) -> AnalysisResult:
    """분석 실패를 빈 결과로 바꾸지 않고 HTTP 오류로 유지합니다."""

    try:
        return await analysis
    except (GraphAnalysisUnavailable, ServiceUnavailable, SessionExpired, AuthError):
        logger.error("Track 1 graph service unavailable")
        raise HTTPException(status_code=503, detail="Graph analysis unavailable") from None
    except Neo4jError as exc:
        if "ProcedureNotFound" in (exc.code or ""):
            logger.error("Track 1 GDS procedure unavailable")
            raise HTTPException(status_code=503, detail="Graph analysis unavailable") from None
        logger.error("Track 1 graph analysis failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Graph analysis failed") from None
    except GraphResultInvalid:
        logger.error("Track 1 graph result validation failed")
        raise HTTPException(status_code=500, detail="Graph analysis result invalid") from None
    except Exception as exc:
        logger.error("Track 1 graph analysis failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Graph analysis failed") from None


@router.post("/analyze", response_model=Track1AnalysisResponse)
async def analyze_louvain_clusters(
    request: Track1AnalysisRequest,
    graph_service: GraphService = Depends(get_graph_service),
):
    return await _run_analysis(graph_service.analyze_louvain(request))


@v2_router.post("/analyze", response_model=Track1LimitAnalysisResponse)
async def analyze_louvain_clusters_with_limits(
    request: Track1LimitAnalysisRequest,
    graph_service: GraphService = Depends(get_graph_service),
):
    return await _run_analysis(graph_service.analyze_louvain_with_limits(request))


@bulk_router.post("", response_model=Track1BulkJobAccepted, status_code=202)
async def create_bulk_analysis_job(
    request: Track1BulkJobCreateRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
    service=Depends(get_track1_bulk_job_service),
):
    """Register a fixed private NDJSON artifact; ticket rows are never read from this HTTP body."""
    return service.submit(request, idempotency_key)


@bulk_router.get("/{job_id}", response_model=Track1BulkJobStatus)
async def get_bulk_analysis_job(job_id: str, service=Depends(get_track1_bulk_job_service)):
    return service.repository.status(job_id)


@bulk_router.post("/{job_id}/retry", response_model=Track1BulkJobAccepted, status_code=202)
async def retry_bulk_analysis_job(job_id: str, service=Depends(get_track1_bulk_job_service)):
    return service.retry(job_id)


@bulk_router.post("/{job_id}/cancel", response_model=Track1BulkJobStatus, status_code=202)
async def cancel_bulk_analysis_job(job_id: str, service=Depends(get_track1_bulk_job_service)):
    return service.repository.cancel(job_id)


@bulk_router.get("/{job_id}/results", response_model=Track1BulkJobResults)
async def get_bulk_analysis_results(
    job_id: str,
    cursor: str | None = Query(default=None, pattern=r"^\d+$"),
    limit: int = Query(default=20, ge=1, le=100),
    member_limit: int = Query(default=100, ge=1, le=500),
    service=Depends(get_track1_bulk_job_service),
):
    return service.repository.results(job_id, cursor, limit, member_limit)


@bulk_router.get("/{job_id}/results/{cluster_id}/members", response_model=Track1BulkClusterMemberPage)
async def get_bulk_cluster_members(
    job_id: str,
    cluster_id: str,
    cursor: str | None = Query(default=None, pattern=r"^\d+$"),
    limit: int = Query(default=100, ge=1, le=500),
    service=Depends(get_track1_bulk_job_service),
):
    return service.repository.member_page(job_id, cluster_id, cursor, limit)


@router.post("/macro", include_in_schema=False)
async def legacy_macro_removed():
    """레거시 WCC 경로는 본문을 읽거나 그래프에 적재하지 않고 항상 410으로 차단합니다."""
    return JSONResponse(
        status_code=410,
        content={"detail": [{"code": "LEGACY_ENDPOINT_REMOVED"}]},
        headers={"Cache-Control": "no-store"},
    )
