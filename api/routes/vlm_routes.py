import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from schemas.dto import VLMResponse, VLMParsedData
from core.dependencies import get_vlm_service
from services.vlm_service import VLMService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/parse", tags=["Track 2 (VLM)"])

class ImageParseRequest(BaseModel):
    image_base64: str = Field(..., description="Base64 인코딩된 티켓 이미지 데이터 (헤더 제외)")
    mime_type: str = Field(default="image/jpeg", description="이미지 MIME 타입")

@router.post("/ticket-image", response_model=VLMResponse)
async def parse_ticket_image_endpoint(
    request: ImageParseRequest,
    vlm_service: VLMService = Depends(get_vlm_service)
):
    """
    [Track 2] 제보된 티켓 이미지를 VLM으로 비동기 분석하여 
    구역(Zone), 열(Row), 좌석(Seat) 정보를 순수 JSON 형태로 추출합니다.
    """
    logger.info("[Track 2] VLM 티켓 이미지 파싱 요청 수신")
    
    try:
        parsed_dict = await vlm_service.parse_ticket_image(
            base64_image=request.image_base64,
            mime_type=request.mime_type
        )
        
        vlm_data = VLMParsedData(**parsed_dict)
        
        return VLMResponse(
            status="success",
            message="티켓 좌석 정보 파싱 완료",
            parsed_data=vlm_data
        )
    except Exception as e:
        logger.error(f"[Track 2] VLM 파싱 처리 중 서버 에러: {e}")
        raise HTTPException(status_code=500, detail="이미지 파싱 중 외부 API 통신 오류가 발생했습니다.")