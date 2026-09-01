import os
import logging
import threading
from fastapi import HTTPException
from fraud_detector import TicketFraudDetector

logger = logging.getLogger(__name__)

_detector_instance = None
_lock = threading.Lock()

def get_fraud_detector() -> TicketFraudDetector:
    """
    FastAPI 의존성 주입을 위한 TicketFraudDetector 싱글톤 인스턴스를 반환합니다.
    최초 호출 시에만 데이터베이스 커넥션을 생성하여 리소스를 최적화합니다.

    Returns:
        TicketFraudDetector: 초기화된 그래프 데이터베이스 제어 객체
    
    Raises:
        HTTPException: 데이터베이스 환경변수가 누락된 경우 500 에러 발생
    """
    global _detector_instance
    
    if _detector_instance is None:
        with _lock:
            if _detector_instance is None:
                neo4j_uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
                neo4j_user = os.getenv("NEO4J_USER", "neo4j")
                neo4j_password = os.getenv("NEO4J_PASSWORD")
        
                if not neo4j_password:
                    logger.error("데이터베이스 인증 정보(NEO4J_PASSWORD)가 누락되었습니다.")
                    raise HTTPException(status_code=500, detail="Internal Server Configuration Error")
        
                try:
                    _detector_instance = TicketFraudDetector(neo4j_uri, neo4j_user, neo4j_password)
                    logger.info("Neo4j 데이터베이스 커넥션 풀이 성공적으로 초기화되었습니다.")
                except Exception as e:
                    logger.exception(f"Neo4j 커넥션 풀 생성 중 오류 발생: {e}")
                    raise HTTPException(status_code=500, detail="Database Connection Failed")
            
    return _detector_instance