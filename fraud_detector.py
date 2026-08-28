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
    Neo4j 그래프 데이터베이스와 통신하여 데이터를 적재하고, 티켓 예매 어뷰징 패턴을 탐지합니다.
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

    def clear_graph(self) -> None:
        """
        전체 파이프라인 재실행 시 랜덤 데이터 중복 누적을 방지하기 위해 그래프를 초기화합니다.
        메모리 초과 방지를 위해 삭제 작업도 1만 건 단위 배치 트랜잭션으로 처리합니다.
        """
        clear_query = """
        MATCH (n)
        CALL {
            WITH n
            DETACH DELETE n
        } IN TRANSACTIONS OF 10000 ROWS;
        """
        with self.driver.session() as session:
            session.run(clear_query).consume()
        logger.info("새로운 데이터 적재를 위해 기존 그래프 데이터가 안전하게 초기화되었습니다.")

    def create_indexes(self) -> None:
        """
        10만 건 이상의 대용량 데이터 적재 시 MERGE 연산 병목을 막기 위해 핵심 노드 식별자에 사전 인덱스를 생성합니다.
        """
        index_queries = [
            "CREATE INDEX account_id IF NOT EXISTS FOR (n:Account) ON (n.id)",
            "CREATE INDEX payment_hash IF NOT EXISTS FOR (n:Payment) ON (n.hash)",
            "CREATE INDEX address_hash IF NOT EXISTS FOR (n:Address) ON (n.hash)"
        ]
        
        with self.driver.session() as session:
            for query in index_queries:
                session.run(query).consume()
        logger.info("Neo4j 노드 검색 최적화를 위한 인덱스 생성이 완료되었습니다.")

    def load_csv_to_graph(self) -> None:
        """
        도커 컨테이너 내부에 마운트된 CSV 파일을 읽어와 노드와 관계로 변환합니다.
        메모리 초과 방지를 위해 1만 건씩 트랜잭션을 쪼개서 넣고, 중복 생성을 막기 위해 MERGE를 사용합니다.
        """
        load_query = """
        LOAD CSV WITH HEADERS FROM 'file:///final_secure_ticket_data.csv' AS row
        CALL {
            WITH row
            MERGE (acc:Account {id: row.account_id})
            MERGE (phone:Phone {hash: row.hashed_phone})
            MERGE (pay:Payment {hash: row.hashed_payment_method})
            MERGE (addr:Address {hash: row.hashed_normalized_address})

            MERGE (acc)-[:HAS_PHONE]->(phone)
            MERGE (acc)-[:USED_PAYMENT]->(pay)
            MERGE (acc)-[:DELIVERED_TO]->(addr)
        } IN TRANSACTIONS OF 10000 ROWS;
        """
        
        with self.driver.session() as session:
            session.run(load_query).consume()
        logger.info("CSV 데이터의 노드 및 간선 병합 적재가 완료되었습니다.")

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

    fraud_detector = None
    
    try:
        fraud_detector = TicketFraudDetector(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)

        fraud_detector.create_indexes()
        fraud_detector.load_csv_to_graph()

        suspicious_clusters = fraud_detector.detect_abnormal_payment_clusters(threshold=ABUSING_THRESHOLD)
        
        if suspicious_clusters:
            cluster_dataframe = pd.DataFrame(suspicious_clusters)
            
            cluster_dataframe['masked_hash'] = cluster_dataframe['payment_hash'].apply(lambda x: str(x)[:8] + '***')
            
            logger.info("긴급: 암표 조직 결제 수단 적발 현황 상위 10건")
            logger.info("\n" + cluster_dataframe[['masked_hash', 'account_count']].to_string())
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