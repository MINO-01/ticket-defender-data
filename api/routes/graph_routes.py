import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from neo4j.exceptions import AuthError, Neo4jError, ServiceUnavailable, SessionExpired
from schemas.dto import ClusterResponse, AnalysisRequest, Track1AnalysisRequest, Track1AnalysisResponse
from core.dependencies import get_graph_service
from services.graph_service import GraphAnalysisUnavailable, GraphResultInvalid, GraphService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/clusters", tags=["Track 1 (Macro)"])


@router.post("/analyze", response_model=Track1AnalysisResponse)
async def analyze_louvain_clusters(
    request: Track1AnalysisRequest,
    graph_service: GraphService = Depends(get_graph_service),
):
    try:
        return await graph_service.analyze_louvain(request)
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

@router.post("/macro", response_model=ClusterResponse)
async def analyze_macro_clusters(
    request: AnalysisRequest,
    threshold: int = Query(default=5, ge=2, description="조직망 탐지 최소 계정 수"),
    graph_service: GraphService = Depends(get_graph_service)
):
    """
    [Track 1] 대량의 예매 데이터를 실시간으로 Neo4j에 적재하고, 
    Neo4j GDS WCC 알고리즘을 통해 점조직 형태의 매크로 암표 군집을 색출합니다.
    """
    logger.info(f"거대 암표 조직망 분석 요청 수신: {len(request.tickets)}건 (Threshold: {threshold})")
    
    await graph_service.load_json_to_graph(request.tickets)
    clusters = await graph_service.detect_macro_clusters(threshold=threshold)
    
    return ClusterResponse(
        status="success",
        message=f"매크로 의심 군집 {len(clusters)}개 적발 완료",
        data=clusters
    )
