"""별도 다공연 Track 1 분석 경로입니다."""

from fastapi import APIRouter, Depends, HTTPException

from core.dependencies import get_graph_service
from schemas.multi_event import MultiEventAnalysisRequest, MultiEventAnalysisResponse
from services.multi_event_analysis import analyze_multi_event
from services.graph_service import GraphAnalysisUnavailable, GraphService


router = APIRouter(prefix="/multi-event", tags=["Track 1 (Multi-event investigation)"])


@router.post("/analyze", response_model=MultiEventAnalysisResponse)
async def analyze_multi_event_investigation(
    request: MultiEventAnalysisRequest,
    graph_service: GraphService = Depends(get_graph_service),
):
    """고정된 원장 범위의 조사 후보를 계산하며 분석 오류를 빈 결과로 바꾸지 않습니다."""

    try:
        communities, algorithm_version = await graph_service.analyze_multi_event_exp2(request)
        return analyze_multi_event(request, communities, algorithm_version)
    except ValueError:
        raise HTTPException(status_code=422, detail={"code": "INVALID_MULTI_EVENT_ANALYSIS_INPUT"}) from None
    except GraphAnalysisUnavailable:
        raise HTTPException(status_code=503, detail={"code": "MULTI_EVENT_GDS_UNAVAILABLE"}) from None
    except Exception:
        raise HTTPException(status_code=500, detail={"code": "MULTI_EVENT_ANALYSIS_FAILED"}) from None
