import os
import logging
import pandas as pd
from neo4j.exceptions import AuthError, ClientError, ServiceUnavailable, ConfigurationError
from typing import List, Dict, Any
from neo4j import GraphDatabase
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class TicketFraudDetector:
    """
    네오포제이 그래프 데이터베이스와 통신하여 티켓 예매 어뷰징 패턴을 탐지하는 클래스입니다.
    데이터베이스 커넥션 풀을 관리하고 다중 홉 탐색 질의를 수행합니다.
    """
    
    def __init__(self, uri: str, user: str, password: str) -> None:
        """
        데이터베이스 드라이버 인스턴스를 초기화합니다.
        
        Args:
            uri: 네오포제이 데이터베이스 접속 주소
            user: 인증용 사용자 계정
            password: 인증용 비밀번호
        """
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self) -> None:
        """
        사용이 완료된 데이터베이스 커넥션을 안전하게 종료하고 리소스를 반환합니다.
        """
        self.driver.close()

    def detect_abnormal_payment_clusters(self, threshold: int = 5) -> List[Dict[str, Any]]:
        """
        단일 결제 수단에 다수의 예매 계정이 집중된 어뷰징 군집을 탐지합니다.
        
        Args:
            threshold: 이상 탐지로 간주할 단일 결제 수단 당 최소 연결 계정 수
            
        Returns:
            탐지된 결제 수단 해시값 및 연동된 계정 리스트를 포함하는 딕셔너리 배열
        """
        query = """
        MATCH (acc:Account)-[:USED_PAYMENT]->(pay:Payment)
        WITH pay, count(acc) AS account_count, collect(acc.id) AS accounts
        WHERE account_count >= $threshold
        RETURN pay.hash AS payment_hash, account_count, accounts
        ORDER BY account_count DESC
        LIMIT 10
        """
        
        with self.driver.session() as session:
            result = session.run(query, threshold=threshold)
            return [record.data() for record in result]

if __name__ == "__main__":
    # 인프라 접근 정보 및 비즈니스 로직 임계값 상수화
    NEO4J_URI = "bolt://localhost:7687"
    NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
    NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
    ABUSING_THRESHOLD = 5

    if not NEO4J_PASSWORD:
        logger.error("보안 경고: .env 파일에서 데이터베이스 비밀번호를 찾을 수 없습니다.")
        exit(1)
    
    logger.info("어뷰징 탐지 자동화 모듈 구동을 시작합니다.")
    fraud_detector = None
    
    try:
        fraud_detector = TicketFraudDetector(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        suspicious_clusters = fraud_detector.detect_abnormal_payment_clusters(threshold=ABUSING_THRESHOLD)
        
        if suspicious_clusters:
            cluster_dataframe = pd.DataFrame(suspicious_clusters)
            
            cluster_dataframe['masked_hash'] = cluster_dataframe['payment_hash'].apply(lambda x: str(x)[:8] + '***')
            
            logger.info("긴급: 암표 조직 결제 수단 적발 현황 상위 10건")
            logger.info("\n" + cluster_dataframe[['masked_hash', 'account_count']].to_string())
            
            primary_target_accounts = cluster_dataframe.iloc[0]['accounts']
            masked_accounts = [str(acc)[:6] + "***" for acc in primary_target_accounts[:5]]
            
            logger.info("적발된 계정 리스트 추출이 완료되었습니다. 일괄 계정 차단 프로세스 연동이 가능합니다.")
            logger.info(f"최우선 차단 대상 계정 식별자 상위 5개: {masked_accounts}")
            
        else:
            logger.info("현재 탐지된 이상 결제 군집이 없습니다.")
            
    except (AuthError, ClientError, ServiceUnavailable, ConfigurationError) as db_error:
        logger.exception(f"Neo4j 데이터베이스 연결 또는 쿼리 실행 중 오류가 발생했습니다: {db_error}")
        raise
    except Exception as error:
        logger.exception("탐지 로직 실행 중 예기치 않은 치명적 오류가 발생했습니다.")
        raise
        
    finally:
        if fraud_detector:
            fraud_detector.close()
            logger.info("데이터베이스 커넥션이 안전하게 종료되었습니다.")