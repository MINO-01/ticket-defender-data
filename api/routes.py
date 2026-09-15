import logging
from fastapi import APIRouter, Depends, Query
from schemas.models import ClusterResponse, AnalysisRequest
from core.dependencies import get_fraud_detector
from fraud_detector import TicketFraudDetector

logger = logging.getLogger(__name__)
router = APIRouter()

@router.post("/clusters/payment", response_model=ClusterResponse)
def analyze_payment_clusters(
    request: AnalysisRequest,
    threshold: int = Query(default=5, ge=2, description="군집 탐지 최소 중복 계정 수"),
    detector: TicketFraudDetector = Depends(get_fraud_detector)
):
    """
    [POST] Spring Boot로부터 수신한 대량의 예매 데이터를 Neo4j에 적재하고,
    단일 결제 수단에 다수의 계정이 집중된 어뷰징 군집을 조회하여 반환합니다.
    """
    logger.info(f"Spring Boot로부터 {len(request.tickets)}건의 분석 요청 수신 (threshold: {threshold})")
    
    detector.load_json_to_graph(request.tickets)
    
    clusters = detector.detect_abnormal_payment_clusters(threshold=threshold)
    
    return ClusterResponse(
        status="success",
        message=f"의심 군집 {len(clusters)}건을 성공적으로 조회했습니다.",
        data=clusters
    )