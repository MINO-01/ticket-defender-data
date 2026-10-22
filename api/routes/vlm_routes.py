"""Authenticated internal image parsing endpoints."""

from datetime import datetime, timezone
import logging
from typing import Literal

from fastapi import APIRouter, Depends, Request, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field

from core.dependencies import get_vlm_service
from core.vlm_errors import VLMProblem
from schemas.dto import VLMResponse
from schemas.listing_context import (
    ListingContextRequest, ListingContextResponse, MAX_LISTING_BASE64_LENGTH,
    MAX_LISTING_IMAGE_BYTES, derive_listing_status,
)
from services.image_validation import (
    LEGACY_MAX_BASE64_LENGTH, LEGACY_MAX_IMAGE_BYTES, validate_image,
)
from services.vlm_service import VLMService


logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/parse", tags=["Track 2 (VLM)"],
    dependencies=[Security(APIKeyHeader(name="X-TD-Internal-Key", auto_error=False))],
)


class ImageParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    image_base64: str = Field(min_length=1, max_length=LEGACY_MAX_BASE64_LENGTH)
    mime_type: Literal["image/jpeg", "image/png", "image/webp"] = "image/jpeg"


@router.post("/ticket-image", response_model=VLMResponse)
async def parse_ticket_image_endpoint(
    request: ImageParseRequest,
    vlm_service: VLMService = Depends(get_vlm_service),
):
    try:
        image = validate_image(
            request.image_base64, request.mime_type,
            max_base64_length=LEGACY_MAX_BASE64_LENGTH,
            max_bytes=LEGACY_MAX_IMAGE_BYTES,
            allow_webp=True,
        )
        parsed = await vlm_service.parse_ticket_image(image)
        return VLMResponse(status="success", message="티켓 좌석 정보 파싱 완료", parsed_data=parsed)
    except VLMProblem:
        raise
    except Exception as error:
        logger.error("Ticket image analysis failed: %s", type(error).__name__)
        raise VLMProblem(500, "ANALYSIS_INTERNAL_ERROR", "Analysis failed", "Analysis failed internally") from None


@router.post("/listing-context", response_model=ListingContextResponse, response_model_by_alias=True)
async def parse_listing_context_endpoint(
    payload: ListingContextRequest,
    request: Request,
    vlm_service: VLMService = Depends(get_vlm_service),
):
    if request.state.supplied_trace and request.state.request_id != payload.request_id:
        raise VLMProblem(422, "INVALID_REQUEST", "Invalid request", "Trace and body request IDs differ")
    request.state.request_id = payload.request_id
    try:
        image = validate_image(
            payload.image_base64, payload.mime_type,
            max_base64_length=MAX_LISTING_BASE64_LENGTH,
            max_bytes=MAX_LISTING_IMAGE_BYTES,
            expected_sha256=payload.image_sha256,
            allow_webp=False,
        )
        fields, model_name, model_version = await vlm_service.parse_listing_context(image)
        return ListingContextResponse(
            request_id=payload.request_id,
            evidence_id=payload.evidence_id,
            image_sha256=payload.image_sha256,
            status=derive_listing_status(fields),
            fields=fields,
            model_name=model_name,
            model_version=model_version,
            analyzed_at=datetime.now(timezone.utc),
        )
    except VLMProblem:
        raise
    except Exception as error:
        logger.error("Listing analysis failed: %s", type(error).__name__)
        raise VLMProblem(500, "ANALYSIS_INTERNAL_ERROR", "Analysis failed", "Analysis failed internally") from None
