from fastapi import APIRouter
from schemas.dto import ClusterResponse

router = APIRouter(prefix="/clusters", tags=["Track 1 (Macro)"])

@router.post("/macro", response_model=ClusterResponse)
async def analyze_macro_clusters_placeholder():
    """
    [Phase 1] 거대 조직망 탐지 API
    실제 DB 적재 및 탐지 로직은 추후 구현 예정입니다.
    """
    return ClusterResponse(
        status="pending",
        message="Phase 1 아키텍처 뼈대입니다. 추후 구현 예정입니다.",
        data=[]
    )