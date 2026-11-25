"""별도 다공연 Track 1 분석 경로입니다."""

from typing import Awaitable, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from core.dependencies import get_graph_service_factory, get_multi_event_artifact_job_service
from schemas.multi_event import (
    MultiEventAnalysisRequest,
    MultiEventAnalysisResponse,
    MultiEventArtifactJobAccepted,
    MultiEventArtifactJobCreateRequest,
    MultiEventArtifactJobStatus,
    MultiEventCandidatePage,
    MultiEventMemberPage,
    MultiEventRelationshipPage,
    UUIDText,
)
from services.multi_event_analysis import analyze_multi_event
from services.graph_service import GraphAnalysisUnavailable, GraphService


router = APIRouter(prefix="/multi-event", tags=["Track 1 (Multi-event investigation)"])


@router.post("/jobs", response_model=MultiEventArtifactJobAccepted, status_code=202)
async def create_multi_event_artifact_job(
    request: MultiEventArtifactJobCreateRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
    service=Depends(get_multi_event_artifact_job_service),
):
    """Register a fixed private NDJSON artifact; input rows are streamed from object storage."""
    return service.submit(request.manifest, idempotency_key)


@router.get("/jobs/{job_id}", response_model=MultiEventArtifactJobStatus)
async def get_multi_event_artifact_job(
    job_id: UUIDText,
    service=Depends(get_multi_event_artifact_job_service),
):
    return service.repository.status(job_id)


@router.post("/jobs/{job_id}/retry", response_model=MultiEventArtifactJobAccepted, status_code=202)
async def retry_multi_event_artifact_job(
    job_id: UUIDText,
    service=Depends(get_multi_event_artifact_job_service),
):
    return service.retry(job_id)


@router.get("/jobs/{job_id}/results", response_model=MultiEventCandidatePage)
async def get_multi_event_artifact_candidates(
    job_id: UUIDText,
    cursor: str | None = Query(default=None, pattern=r"^\d+$", max_length=10),
    limit: int = Query(default=100, ge=1, le=500),
    service=Depends(get_multi_event_artifact_job_service),
):
    return service.repository.candidate_page(job_id, cursor, limit)


@router.get("/jobs/{job_id}/results/{candidate_id}/members", response_model=MultiEventMemberPage)
async def get_multi_event_artifact_candidate_members(
    job_id: UUIDText,
    candidate_id: str,
    cursor: str | None = Query(default=None, pattern=r"^\d+$", max_length=10),
    limit: int = Query(default=100, ge=1, le=500),
    service=Depends(get_multi_event_artifact_job_service),
):
    return service.repository.member_page(job_id, candidate_id, cursor, limit)


@router.get("/jobs/{job_id}/results/{candidate_id}/relationships", response_model=MultiEventRelationshipPage)
async def get_multi_event_artifact_candidate_relationships(
    job_id: UUIDText,
    candidate_id: str,
    cursor: str | None = Query(default=None, pattern=r"^\d+$", max_length=10),
    limit: int = Query(default=100, ge=1, le=500),
    service=Depends(get_multi_event_artifact_job_service),
):
    return service.repository.relationship_page(job_id, candidate_id, cursor, limit)


@router.post("/analyze", response_model=MultiEventAnalysisResponse)
async def analyze_multi_event_investigation(
    request: MultiEventAnalysisRequest,
    graph_service_factory: Callable[[], Awaitable[GraphService]] = Depends(get_graph_service_factory),
):
    """고정된 원장 범위의 조사 후보를 계산하며 분석 오류를 빈 결과로 바꾸지 않습니다."""

    try:
        graph_service = await graph_service_factory()
        communities, algorithm_version = await graph_service.analyze_multi_event_exp2(request)
        return analyze_multi_event(request, communities, algorithm_version)
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=422, detail={"code": "INVALID_MULTI_EVENT_ANALYSIS_INPUT"}) from None
    except GraphAnalysisUnavailable:
        raise HTTPException(status_code=503, detail={"code": "MULTI_EVENT_GDS_UNAVAILABLE"}) from None
    except Exception:
        raise HTTPException(status_code=500, detail={"code": "MULTI_EVENT_ANALYSIS_FAILED"}) from None
