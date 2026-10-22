import os
import logging
import asyncio
from fastapi import HTTPException
from dotenv import load_dotenv

load_dotenv()

from services.graph_service import GraphService
from services.vlm_service import VLMService
from services.vlm_service import GeminiProvider
from core.vlm_errors import VLMProblem

logger = logging.getLogger(__name__)

_graph_service_instance = None
_vlm_service_instance = None
_lock = asyncio.Lock()

async def get_graph_service() -> GraphService:
    """
    FastAPI 의존성 주입(DI)을 위한 GraphService 비동기 싱글톤 인스턴스를 반환합니다.
    
    서버 기동 시 최초 호출에만 Neo4j 데이터베이스 커넥션 풀을 생성하여 리소스를 최적화하며,
    Double-Checked Locking 패턴과 asyncio.Lock을 결합하여 동시성 이슈를 완벽히 제어합니다.

    Returns:
        GraphService: 초기화된 비동기 그래프 데이터베이스 제어 객체
    
    Raises:
        HTTPException: 데이터베이스 환경변수가 누락되거나 커넥션 생성 실패 시 500 에러 발생
    """
    global _graph_service_instance
    
    if _graph_service_instance is None:
        async with _lock:
            if _graph_service_instance is None:
                uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
                user = os.getenv("NEO4J_USER", "neo4j")
                pwd = os.getenv("NEO4J_PASSWORD")
                
                if not pwd:
                    logger.error("보안 경고: 데이터베이스 인증 정보(NEO4J_PASSWORD)가 누락되었습니다.")
                    raise HTTPException(status_code=500, detail="Internal Server Configuration Error")
                
                try:
                    _graph_service_instance = GraphService(uri, user, pwd)
                    logger.info("GraphService(Neo4j) 비동기 커넥션 풀이 성공적으로 초기화되었습니다.")
                except Exception as e:
                    logger.error("GraphService 커넥션 풀 생성 중 오류 발생: %s", type(e).__name__)
                    raise HTTPException(status_code=500, detail="Database Connection Failed")
                    
    return _graph_service_instance

async def get_vlm_service() -> VLMService:
    """Return a provider only after explicit external-transfer configuration."""
    global _vlm_service_instance

    if os.getenv("VLM_EXTERNAL_CALLS_ENABLED", "false").lower() != "true" or os.getenv("VLM_PROVIDER") != "gemini":
        raise VLMProblem(503, "MODEL_PROVIDER_UNCONFIGURED", "Analysis unavailable", "Model provider is unavailable")
    if _vlm_service_instance is None:
        async with _lock:
            if _vlm_service_instance is None:
                api_key = os.getenv("GEMINI_API_KEY")
                if not api_key:
                    raise VLMProblem(503, "VLM_CONFIGURATION_ERROR", "Analysis unavailable", "Model configuration is unavailable")
                try:
                    model_name = os.getenv("VLM_MODEL", "gemini-3.8-flash")
                    if not model_name or len(model_name) > 128:
                        raise ValueError("invalid model")
                    concurrency = int(os.getenv("VLM_MAX_CONCURRENT", "2"))
                    _vlm_service_instance = VLMService(
                        GeminiProvider(api_key=api_key, model_name=model_name), max_concurrent=concurrency,
                    )
                except Exception as error:
                    logger.error("VLM provider configuration invalid: %s", type(error).__name__)
                    raise VLMProblem(503, "VLM_CONFIGURATION_ERROR", "Analysis unavailable", "Model configuration is unavailable") from None
    return _vlm_service_instance
