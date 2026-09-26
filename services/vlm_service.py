import logging
import json
import asyncio
import base64
from typing import Dict, Any
from google import genai
from google.genai import types
from google.genai import errors as genai_errors

logger = logging.getLogger(__name__)

class VLMService:
    def __init__(self, api_key: str):
        """VLM 객체 초기화"""
        self.client = genai.Client(api_key=api_key)
        self.model_name = "gemini-3.8-flash"

    async def parse_ticket_image(self, base64_image: str, mime_type: str = "image/jpeg") -> Dict[str, Any]:
        """
        [Track 2] 비동기 AI 파싱 및 지수 백오프 재시도 로직
        """
        prompt = """
        너는 대한민국의 공연/스포츠 티켓 좌석 정보를 정확하게 파싱하는 시스템이야.
        주어진 티켓 이미지에서 '구역(zone)', '열(row)', '좌석 번호(seat)' 정보를 추출해.
        해당 정보가 이미지에 없다면 null을 반환해.
        반드시 아래 JSON 형식으로만 응답해야 해. 다른 설명이나 마크다운은 절대 추가하지 마.
        {"zone": "A", "row": "10", "seat": "15"}
        """

        image_bytes = base64.b64decode(base64_image, validate=True)

        contents = [
            prompt,
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
        ]

        max_retries = 3
        timeout_seconds = 15.0

        for attempt in range(1, max_retries + 1):
            try:
                response = await asyncio.wait_for(
                    self.client.aio.models.generate_content(
                        model=self.model_name,
                        contents=contents,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            temperature=0.0,
                        )
                    ),
                    timeout=timeout_seconds
                )
                
                parsed_json = json.loads(response.text)
                logger.info(f"[VLMService] 티켓 이미지 파싱 성공 (시도: {attempt}/{max_retries})")
                return parsed_json

            except asyncio.TimeoutError:
                logger.warning(f"[VLMService] VLM API 타임아웃 발생 (시도: {attempt}/{max_retries})")
            except json.JSONDecodeError as e:
                truncated_response = response.text[:100].replace('\n', ' ')
                logger.error(f"[VLMService] VLM 환각 발생 - JSON 파싱 실패: {e} | 응답: {truncated_response}")
            except genai_errors.APIError as e:
                if e.code not in (429, 500, 502, 503, 504):
                    raise
                logger.warning(f"[VLMService] VLM API 호출 오류: {e} (시도: {attempt}/{max_retries})")

            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)

        logger.error("[VLMService] 최대 재시도 횟수 초과. VLM 파싱 실패.")
        raise RuntimeError("VLM API 호출 실패 및 타임아웃")