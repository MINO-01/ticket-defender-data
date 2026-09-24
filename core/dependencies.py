import os
import logging
import asyncio
from fastapi import HTTPException
from services.graph_service import GraphService
from services.vlm_service import VLMService

logger = logging.getLogger(__name__)

_graph_service_instance = None
_vlm_service_instance = None
_lock = asyncio.Lock()

async def get_graph_service() -> GraphService:
    """
    FastAPI 의존성 주입을 위한 GraphService 비동기 싱글톤 인스턴스를 반환합니다.
    
    서버 기동 시 최초 호출에만 Neo4j 데이터베이스 커넥션을 생성하여 리소스를 최적화하며,
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
                    logger.info("GraphService Phase 1 셸이 성공적으로 초기화되었습니다.")
                except Exception as e:
                    logger.exception(f"GraphService 커넥션 풀 생성 중 오류 발생: {e}")
                    raise HTTPException(status_code=500, detail="Database Connection Failed")
                    
    return _graph_service_instance

async def get_vlm_service() -> VLMService:
    """
    FastAPI 의존성 주입을 위한 VLMService 비동기 싱글톤 인스턴스를 반환합니다.
    
    외부 AI API 호출을 담당하는 서비스를 관리하며,
    메모리 낭비를 막기 위해 단일 객체로 유지합니다.

    Returns:
        VLMService: VLM 외부 API 비동기 통신을 담당하는 서비스 객체
    """
    global _vlm_service_instance
    
    if _vlm_service_instance is None:
        async with _lock:
            if _vlm_service_instance is None:
                _vlm_service_instance = VLMService()
                logger.info("VLMService 싱글톤 객체가 성공적으로 초기화되었습니다.")
                
    return _vlm_service_instance