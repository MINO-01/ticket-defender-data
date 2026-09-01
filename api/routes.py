import logging
from fastapi import APIRouter, Depends, Query
from schemas.response import ClusterResponse
from core.dependencies import get_fraud_detector
from fraud_detector import TicketFraudDetector

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/clusters/payment", response_model=ClusterResponse)
def get_payment_clusters(
    threshold: int = Query(default=5, ge=2, description="군집 탐지 최소 중복 계정 수"),
    detector: TicketFraudDetector = Depends(get_fraud_detector)
):
    """
    단일 결제 수단에 다수의 계정이 집중된 어뷰징 군집을 조회합니다.
    
    Args:
        threshold: 이상 탐지로 간주할 최소 연결 계정 수 (기본값: 5)
        detector: 의존성 주입된 DB 제어 객체
        
    Returns:
        ClusterResponse: 상태 메시지와 탐지된 군집 리스트를 포함한 JSON 응답
    """
    logger.info(f"결제 수단 기준 암표 군집 탐지 요청 수신 (threshold: {threshold})")
    clusters = detector.detect_abnormal_payment_clusters(threshold=threshold)
    
    return ClusterResponse(
        status="success",
        message=f"의심 군집 {len(clusters)}건을 성공적으로 조회했습니다.",
        data=clusters
    )